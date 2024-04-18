"""
Construct and serialize the Bitcoin transactions that are Counterparty transactions.

This module contains no consensus‐critical code.
"""

import binascii
import decimal
import hashlib
import io
import json  # noqa: F401
import logging
import math  # noqa: F401
import os  # noqa: F401
import re  # noqa: F401
import sys  # noqa: F401
import threading
import time  # noqa: F401

import bitcoin as bitcoinlib
import cachetools
import requests  # noqa: F401
from bitcoin.core import CTransaction, b2lx, x  # noqa: F401
from bitcoin.core.script import CScript  # noqa: F401

from counterpartycore.lib import (
    arc4,  # noqa: F401
    backend,
    blocks,  # noqa: F401
    config,
    exceptions,
    gettxinfo,
    ledger,
    script,
    util,
)
from counterpartycore.lib.kickstart.blocks_parser import BlockchainParser
from counterpartycore.lib.transaction_helper import p2sh_encoding, serializer

# Constants
OP_RETURN = b"\x6a"
OP_PUSHDATA1 = b"\x4c"
OP_DUP = b"\x76"
OP_HASH160 = b"\xa9"
OP_EQUALVERIFY = b"\x88"
OP_CHECKSIG = b"\xac"
OP_0 = b"\x00"
OP_1 = b"\x51"
OP_2 = b"\x52"
OP_3 = b"\x53"
OP_CHECKMULTISIG = b"\xae"
OP_EQUAL = b"\x87"

D = decimal.Decimal


# FILE AS SINGLETON API
#
# We eventually neet to rip this out and just instantiate the TransactionService
# when we explcitly build up a dependency tree.

TRANSACTION_SERVICE_SINGLETON = None


def initialise(force=False):
    global TRANSACTION_SERVICE_SINGLETON  # noqa: PLW0603

    if not force and TRANSACTION_SERVICE_SINGLETON:
        return TRANSACTION_SERVICE_SINGLETON

    utxo_locks = None
    if config.UTXO_LOCKS_MAX_ADDRESSES > 0:
        utxo_locks = util.DictCache(size=config.UTXO_LOCKS_MAX_ADDRESSES)

    TRANSACTION_SERVICE_SINGLETON = TransactionService(
        backend=backend,
        prefix=config.PREFIX,
        ps2h_dust_return_pubkey=config.P2SH_DUST_RETURN_PUBKEY,
        utxo_locks_max_age=config.UTXO_LOCKS_MAX_AGE,
        utxo_locks_max_addresses=config.UTXO_LOCKS_MAX_ADDRESSES,
        default_regular_dust_size=config.DEFAULT_REGULAR_DUST_SIZE,
        default_multisig_dust_size=config.DEFAULT_MULTISIG_DUST_SIZE,
        estimate_fee_mode=config.ESTIMATE_FEE_MODE,
        op_return_max_size=config.OP_RETURN_MAX_SIZE,
        utxo_locks=utxo_locks,
        utxo_p2sh_encoding_locks=ThreadSafeTTLCache(10000, 180),
        utxo_p2sh_encoding_locks_cache=ThreadSafeTTLCache(1000, 600),
    )


def construct(
    db,
    tx_info,
    encoding="auto",
    fee_per_kb=config.DEFAULT_FEE_PER_KB,
    estimate_fee_per_kb=None,
    estimate_fee_per_kb_nblocks=config.ESTIMATE_FEE_CONF_TARGET,
    regular_dust_size=config.DEFAULT_REGULAR_DUST_SIZE,
    multisig_dust_size=config.DEFAULT_MULTISIG_DUST_SIZE,
    op_return_value=config.DEFAULT_OP_RETURN_VALUE,
    exact_fee=None,
    fee_provided=0,
    provided_pubkeys=None,
    dust_return_pubkey=None,
    allow_unconfirmed_inputs=False,
    unspent_tx_hash=None,
    custom_inputs=None,
    disable_utxo_locks=False,
    extended_tx_info=False,
    old_style_api=None,
    segwit=False,
    p2sh_source_multisig_pubkeys=None,
    p2sh_source_multisig_pubkeys_required=None,
    p2sh_pretx_txid=None,
):
    if TRANSACTION_SERVICE_SINGLETON is None:
        raise Exception("Transaction not initialized")

    return TRANSACTION_SERVICE_SINGLETON.construct(
        db,
        tx_info,
        encoding,
        fee_per_kb,
        estimate_fee_per_kb,
        estimate_fee_per_kb_nblocks,
        regular_dust_size,
        multisig_dust_size,
        op_return_value,
        exact_fee,
        fee_provided,
        provided_pubkeys,
        dust_return_pubkey,
        allow_unconfirmed_inputs,
        unspent_tx_hash,
        custom_inputs,
        disable_utxo_locks,
        extended_tx_info,
        old_style_api,
        segwit,
        p2sh_source_multisig_pubkeys,
        p2sh_source_multisig_pubkeys_required,
        p2sh_pretx_txid,
    )


class BaseThreadSafeCache:
    def __init__(self, *args, **kwargs):
        # Note: reading is thread safe out of the box
        self.lock = threading.Lock()
        self.__cache = self.create_cache(*args, **kwargs)

    def create_cache(self, *args, **kwargs):
        raise NotImplementedError

    def get(self, key, default=None):
        return self.__cache.get(key, default)

    def pop(self, key, default=None):
        with self.lock:
            return self.__cache.pop(key, default)

    def delete(self, key):
        with self.lock:
            try:
                del self.__cache[key]
            except KeyError:
                pass

    def _get_cache(self):
        return self.__cache

    def set(self, key, value):
        with self.lock:
            try:
                self.__cache[key] = value
            except KeyError:
                pass

    def keys(self):
        return self.__cache.keys()

    def __len__(self):
        return len(self.__cache)

    def __iter__(self):
        return iter(self.__cache)

    def __contains__(self, key):
        return key in self.__cache


class ThreadSafeTTLCache(BaseThreadSafeCache):
    def create_cache(self, *args, **kwargs):
        return cachetools.TTLCache(*args, **kwargs)


# set higher than the max number of UTXOs we should expect to
# manage in an aging cache for any one source address, at any one period
# UTXO_P2SH_ENCODING_LOCKS is TTLCache for UTXOs that are used for chaining p2sh encoding
#  instead of a simple (txid, vout) key we use [(vin.prevout.hash, vin.prevout.n) for vin tx1.vin]
# we cache the make_outkey_vin to avoid having to fetch raw txs too often


class TransactionService:
    def __init__(
        self,
        backend,
        prefix,
        ps2h_dust_return_pubkey,
        utxo_locks_max_age=3.0,
        utxo_locks_max_addresses=1000,
        utxo_locks_per_address_maxsize=5000,
        default_regular_dust_size=config.DEFAULT_REGULAR_DUST_SIZE,
        default_multisig_dust_size=config.DEFAULT_MULTISIG_DUST_SIZE,
        estimate_fee_mode=config.ESTIMATE_FEE_MODE,
        op_return_max_size=config.OP_RETURN_MAX_SIZE,
        utxo_p2sh_encoding_locks=None,
        utxo_p2sh_encoding_locks_cache=None,
        utxo_locks=None,
    ):
        self.logger = logging.getLogger(
            config.LOGGER_NAME
        )  # has to be config.LOGGER_NAME or integration tests fail
        self.backend = backend

        self.utxo_p2sh_encoding_locks = utxo_p2sh_encoding_locks or ThreadSafeTTLCache(10000, 180)
        self.utxo_p2sh_encoding_locks_cache = utxo_p2sh_encoding_locks_cache or ThreadSafeTTLCache(
            1000, 600
        )
        self.utxo_locks = utxo_locks

        self.utxo_locks_max_age = utxo_locks_max_age
        self.utxo_locks_max_addresses = utxo_locks_max_addresses
        self.utxo_locks_per_address_maxsize = utxo_locks_per_address_maxsize

        self.default_regular_dust_size = default_regular_dust_size
        self.default_multisig_dust_size = default_multisig_dust_size
        self.estimate_fee_mode = estimate_fee_mode

        self.prefix = prefix
        self.op_return_max_size = op_return_max_size
        self.ps2h_dust_return_pubkey = ps2h_dust_return_pubkey

    def get_dust_return_pubkey(self, source, provided_pubkeys, encoding):
        """Return the pubkey to which dust from data outputs will be sent.

        This pubkey is used in multi-sig data outputs (as the only real pubkey) to
        make those the outputs spendable. It is derived from the source address, so
        that the dust is spendable by the creator of the transaction.
        """
        # Get hex dust return pubkey.
        # inject `script`
        if script.is_multisig(source):
            a, self_pubkeys, b = script.extract_array(
                self.backend.multisig_pubkeyhashes_to_pubkeys(source, provided_pubkeys)
            )
            dust_return_pubkey_hex = self_pubkeys[0]
        else:
            dust_return_pubkey_hex = self.backend.pubkeyhash_to_pubkey(source, provided_pubkeys)

        # Convert hex public key into the (binary) dust return pubkey.
        try:
            dust_return_pubkey = binascii.unhexlify(dust_return_pubkey_hex)
        except binascii.Error:
            raise script.InputError("Invalid private key.")  # noqa: B904

        return dust_return_pubkey

    def make_outkey_vin_txid(self, txid, vout):
        if (txid, vout) not in self.utxo_p2sh_encoding_locks_cache:
            txhex = self.backend.getrawtransaction(txid, verbose=False)
            self.utxo_p2sh_encoding_locks_cache.set((txid, vout), make_outkey_vin(txhex, vout))

        return self.utxo_p2sh_encoding_locks_cache.get((txid, vout))

    def construct_coin_selection(
        self,
        encoding,
        data_array,
        source,
        allow_unconfirmed_inputs,
        unspent_tx_hash,
        custom_inputs,
        fee_per_kb,
        estimate_fee_per_kb,
        estimate_fee_per_kb_nblocks,
        exact_fee,
        size_for_fee,
        fee_provided,
        destination_btc_out,
        data_btc_out,
        regular_dust_size,
        disable_utxo_locks,
    ):
        # Array of UTXOs, as retrieved by listunspent function from bitcoind
        if custom_inputs:
            use_inputs = unspent = custom_inputs
        else:
            if unspent_tx_hash is not None:
                unspent = self.backend.get_unspent_txouts(
                    source,
                    unconfirmed=allow_unconfirmed_inputs,
                    unspent_tx_hash=unspent_tx_hash,
                )
            else:
                unspent = self.backend.get_unspent_txouts(
                    source, unconfirmed=allow_unconfirmed_inputs
                )

            filter_unspents_utxo_locks = []
            if self.utxo_locks is not None and source in self.utxo_locks:
                filter_unspents_utxo_locks = self.utxo_locks[source].keys()
            filter_unspents_p2sh_locks = self.utxo_p2sh_encoding_locks.keys()  # filter out any locked UTXOs to prevent creating transactions that spend the same UTXO when they're created at the same time
            filtered_unspent = []
            for output in unspent:
                if (
                    make_outkey(output) not in filter_unspents_utxo_locks
                    and self.make_outkey_vin_txid(output["txid"], output["vout"])
                    not in filter_unspents_p2sh_locks
                ):
                    filtered_unspent.append(output)
            unspent = filtered_unspent

            if encoding == "multisig":
                dust = self.default_multisig_dust_size
            else:
                dust = self.default_regular_dust_size

            unspent = self.backend.sort_unspent_txouts(unspent, dust_size=dust)
            # self.logger.debug(f"Sorted candidate UTXOs: {[print_coin(coin) for coin in unspent]}")
            use_inputs = unspent

        # use backend estimated fee_per_kb
        if estimate_fee_per_kb:
            estimated_fee_per_kb = self.backend.fee_per_kb(
                estimate_fee_per_kb_nblocks, config.ESTIMATE_FEE_MODE
            )
            if estimated_fee_per_kb is not None:
                fee_per_kb = max(
                    estimated_fee_per_kb, fee_per_kb
                )  # never drop below the default fee_per_kb

        self.logger.debug(f"Fee/KB {fee_per_kb / config.UNIT:.8f}")

        inputs = []
        btc_in = 0
        change_quantity = 0
        sufficient_funds = False
        final_fee = fee_per_kb
        desired_input_count = 1

        if encoding == "multisig" and data_array and ledger.enabled("bytespersigop"):
            desired_input_count = len(data_array) * 2

        # pop inputs until we can pay for the fee
        use_inputs_index = 0
        for coin in use_inputs:
            self.logger.debug(f"New input: {print_coin(coin)}")
            inputs.append(coin)
            btc_in += round(coin["amount"] * config.UNIT)

            # If exact fee is specified, use that. Otherwise, calculate size of tx
            # and base fee on that (plus provide a minimum fee for selling BTC).
            size = 181 * len(inputs) + size_for_fee + 10
            if exact_fee:
                final_fee = exact_fee
            else:
                necessary_fee = int(size / 1000 * fee_per_kb)
                final_fee = max(fee_provided, necessary_fee)
                self.logger.debug(
                    f"final_fee inputs: {len(inputs)} size: {size} final_fee {final_fee}"
                )

            # Check if good.
            btc_out = destination_btc_out + data_btc_out
            change_quantity = btc_in - (btc_out + final_fee)
            self.logger.debug(
                f"Size: {size} Fee: {final_fee / config.UNIT:.8f} Change quantity: {change_quantity / config.UNIT:.8f} BTC"
            )

            # If after the sum of all the utxos the change is dust, then it will be added to the miners instead of returning an error
            if (
                (use_inputs_index == len(use_inputs) - 1)
                and (change_quantity > 0)
                and (change_quantity < regular_dust_size)
            ):
                sufficient_funds = True
                final_fee = final_fee + change_quantity
                change_quantity = 0
            # If change is necessary, must not be a dust output.
            elif change_quantity == 0 or change_quantity >= regular_dust_size:
                sufficient_funds = True
                if len(inputs) >= desired_input_count:
                    break

            use_inputs_index = use_inputs_index + 1

        if not sufficient_funds:
            # Approximate needed change, fee by with most recently calculated
            # quantities.
            btc_out = destination_btc_out + data_btc_out
            total_btc_out = btc_out + max(change_quantity, 0) + final_fee

            error_message = f"Insufficient {config.BTC} at address {source}. (Need approximately {total_btc_out / config.UNIT} {config.BTC}.)"
            if not allow_unconfirmed_inputs:
                error_message += " To spend unconfirmed coins, use the flag `--unconfirmed`. (Unconfirmed coins cannot be spent from multi‐sig addresses.)"
            raise exceptions.BalanceError(error_message)

        # Lock the source's inputs (UTXOs) chosen for this transaction
        if self.utxo_locks is not None and not disable_utxo_locks:
            if source not in self.utxo_locks:
                self.utxo_locks[source] = ThreadSafeTTLCache(
                    self.utxo_locks_per_address_maxsize, self.utxo_locks_max_age
                )

            for input in inputs:
                self.utxo_locks[source].set(make_outkey(input), input)

            # list_unspent = [make_outkey(coin) for coin in unspent]
            # list_used = [make_outkey(input) for input in inputs]
            # list_locked = list(self.utxo_locks[source].keys())
            # self.logger.debug(
            #     f"UTXO locks: Potentials ({len(unspent)}): {list_unspent}, Used: {list_used}, locked UTXOs: {list_locked}"
            # )

        # ensure inputs have scriptPubKey
        #   this is not provided by indexd
        inputs = self.backend.ensure_script_pub_key_for_inputs(inputs)

        return inputs, change_quantity, btc_in, final_fee

    def select_any_coin_from_source(
        self, source, allow_unconfirmed_inputs=True, disable_utxo_locks=False
    ):
        """Get the first (biggest) input from the source address"""

        # Array of UTXOs, as retrieved by listunspent function from bitcoind
        unspent = self.backend.get_unspent_txouts(source, unconfirmed=allow_unconfirmed_inputs)

        filter_unspents_utxo_locks = []
        if self.utxo_locks is not None and source in self.utxo_locks:
            filter_unspents_utxo_locks = self.utxo_locks[source].keys()

        # filter out any locked UTXOs to prevent creating transactions that spend the same UTXO when they're created at the same time
        filtered_unspent = []
        for output in unspent:
            if make_outkey(output) not in filter_unspents_utxo_locks:
                filtered_unspent.append(output)
        unspent = filtered_unspent

        # sort
        unspent = self.backend.sort_unspent_txouts(
            unspent, dust_size=config.DEFAULT_REGULAR_DUST_SIZE
        )

        # use the first input
        input = unspent[0]
        if input is None:
            return None

        # Lock the source's inputs (UTXOs) chosen for this transaction
        if self.utxo_locks is not None and not disable_utxo_locks:
            if source not in self.utxo_locks:
                # TODO: dont ref cache tools directly
                self.utxo_locks[source] = cachetools.TTLCache(
                    self.utxo_locks_per_address_maxsize, self.utxo_locks_max_age
                )

            self.utxo_locks[source].set(make_outkey(input), input)

        return input

    def construct(
        self,
        db,
        tx_info,
        encoding="auto",
        fee_per_kb=config.DEFAULT_FEE_PER_KB,
        estimate_fee_per_kb=None,
        estimate_fee_per_kb_nblocks=config.ESTIMATE_FEE_CONF_TARGET,
        regular_dust_size=config.DEFAULT_REGULAR_DUST_SIZE,
        multisig_dust_size=config.DEFAULT_MULTISIG_DUST_SIZE,
        op_return_value=config.DEFAULT_OP_RETURN_VALUE,
        exact_fee=None,
        fee_provided=0,
        provided_pubkeys=None,
        dust_return_pubkey=None,
        allow_unconfirmed_inputs=False,
        unspent_tx_hash=None,
        custom_inputs=None,
        disable_utxo_locks=False,
        extended_tx_info=False,
        old_style_api=None,
        segwit=False,
        p2sh_source_multisig_pubkeys=None,
        p2sh_source_multisig_pubkeys_required=None,
        p2sh_pretx_txid=None,
    ):
        if estimate_fee_per_kb is None:
            estimate_fee_per_kb = config.ESTIMATE_FEE_PER_KB

        # lazy assign from config, because when set as default it's evaluated before it's configured
        if old_style_api is None:
            old_style_api = config.OLD_STYLE_API

        (source, destination_outputs, data) = tx_info

        if dust_return_pubkey:
            dust_return_pubkey = binascii.unhexlify(dust_return_pubkey)

        if p2sh_source_multisig_pubkeys:
            p2sh_source_multisig_pubkeys = [
                binascii.unhexlify(p) for p in p2sh_source_multisig_pubkeys
            ]

        # Source.
        # If public key is necessary for construction of (unsigned)
        # transaction, use the public key provided, or find it from the
        # blockchain.
        if source:
            script.validate(source)

        source_is_p2sh = script.is_p2sh(source)

        # Normalize source
        if script.is_multisig(source):
            source_address = self.backend.multisig_pubkeyhashes_to_pubkeys(source, provided_pubkeys)
        else:
            source_address = source

        # Sanity checks.
        if exact_fee and not isinstance(exact_fee, int):
            raise exceptions.TransactionError("Exact fees must be in satoshis.")
        if not isinstance(fee_provided, int):
            raise exceptions.TransactionError("Fee provided must be in satoshis.")

        """Determine encoding method"""

        if data:
            desired_encoding = encoding
            # Data encoding methods (choose and validate).
            if desired_encoding == "auto":
                if len(data) + len(self.prefix) <= self.op_return_max_size:
                    encoding = "opreturn"
                else:
                    encoding = (
                        "p2sh"
                        if not old_style_api and ledger.enabled("p2sh_encoding")
                        else "multisig"
                    )  # p2sh is not possible with old_style_api

            elif desired_encoding == "p2sh" and not ledger.enabled("p2sh_encoding"):
                raise exceptions.TransactionError("P2SH encoding not enabled yet")

            elif encoding not in ("pubkeyhash", "multisig", "opreturn", "p2sh"):
                raise exceptions.TransactionError("Unknown encoding‐scheme.")
        else:
            # no data
            encoding = None
        self.logger.debug(f"Constructing {encoding} transaction from {source}.")

        """Destinations"""

        # Destination outputs.
        # Replace multi‐sig addresses with multi‐sig pubkeys. Check that the
        # destination output isn’t a dust output. Set null values to dust size.
        destination_outputs_new = []
        if encoding != "p2sh":
            for address, value in destination_outputs:
                # Value.
                if script.is_multisig(address):
                    dust_size = multisig_dust_size
                else:
                    dust_size = regular_dust_size
                if value == None:  # noqa: E711
                    value = dust_size  # noqa: PLW2901
                elif value < dust_size:
                    raise exceptions.TransactionError("Destination output is dust.")

                # Address.
                script.validate(address)
                if script.is_multisig(address):
                    destination_outputs_new.append(
                        (
                            self.backend.multisig_pubkeyhashes_to_pubkeys(
                                address, provided_pubkeys
                            ),
                            value,
                        )
                    )
                else:
                    destination_outputs_new.append((address, value))

        destination_outputs = destination_outputs_new
        destination_btc_out = sum([value for address, value in destination_outputs])

        """Data"""

        if data:
            # @TODO: p2sh encoding require signable dust key
            if encoding == "multisig":
                # dust_return_pubkey should be set or explicitly set to False to use the default configured for the node
                #  the default for the node is optional so could fail
                if (source_is_p2sh and dust_return_pubkey is None) or (
                    dust_return_pubkey is False and self.ps2h_dust_return_pubkey is None
                ):
                    raise exceptions.TransactionError(
                        "Can't use multisig encoding when source is P2SH and no dust_return_pubkey is provided."
                    )
                elif dust_return_pubkey is False:
                    dust_return_pubkey = binascii.unhexlify(self.ps2h_dust_return_pubkey)

            if not dust_return_pubkey:
                if encoding == "multisig" or encoding == "p2sh" and not source_is_p2sh:
                    dust_return_pubkey = self.get_dust_return_pubkey(
                        source, provided_pubkeys, encoding
                    )
                else:
                    dust_return_pubkey = None

            # Divide data into chunks.
            if encoding == "pubkeyhash":
                # Prefix is also a suffix here.
                chunk_size = 20 - 1 - 8
            elif encoding == "multisig":
                # Two pubkeys, minus length byte, minus prefix, minus two nonces,
                # minus two sign bytes.
                chunk_size = (33 * 2) - 1 - 8 - 2 - 2
            elif encoding == "p2sh":
                pubkeylength = -1
                if dust_return_pubkey is not None:
                    pubkeylength = len(dust_return_pubkey)

                chunk_size = p2sh_encoding.maximum_data_chunk_size(pubkeylength)
            elif encoding == "opreturn":
                chunk_size = config.OP_RETURN_MAX_SIZE
                if len(data) + len(self.prefix) > chunk_size:
                    raise exceptions.TransactionError("One `OP_RETURN` output per transaction.")
            data_array = list(chunks(data, chunk_size))

            # Data outputs.
            if encoding == "multisig":
                data_value = multisig_dust_size
            elif encoding == "p2sh":
                data_value = 0  # this will be calculated later
            elif encoding == "opreturn":
                data_value = op_return_value
            else:
                # Pay‐to‐PubKeyHash, e.g.
                data_value = regular_dust_size
            data_output = (data_array, data_value)

        else:
            data_value = 0
            data_array = []
            data_output = None
            dust_return_pubkey = None

        data_btc_out = data_value * len(data_array)
        self.logger.debug(
            f"data_btc_out={data_btc_out} (data_value={data_value} len(data_array)={len(data_array)})"
        )

        """Inputs"""
        btc_in = 0
        final_fee = 0
        # Calculate collective size of outputs, for fee calculation.
        p2pkhsize = 25 + 9
        if encoding == "multisig":
            data_output_size = 81  # 71 for the data
        elif encoding == "opreturn":
            # prefix + data + 10 bytes script overhead
            data_output_size = len(self.prefix) + 10
            if data is not None:
                data_output_size = data_output_size + len(data)
        else:
            data_output_size = p2pkhsize  # Pay‐to‐PubKeyHash (25 for the data?)

        if encoding == "p2sh":
            # calculate all the p2sh outputs
            size_for_fee, datatx_necessary_fee, data_value, data_btc_out = (
                p2sh_encoding.calculate_outputs(
                    destination_outputs, data_array, fee_per_kb, exact_fee
                )
            )
            # replace the data value
            data_output = (data_array, data_value)
        else:
            sum_data_output_size = len(data_array) * data_output_size
            size_for_fee = ((25 + 9) * len(destination_outputs)) + sum_data_output_size

        if not (encoding == "p2sh" and p2sh_pretx_txid):
            inputs, change_quantity, n_btc_in, n_final_fee = self.construct_coin_selection(
                encoding,
                data_array,
                source,
                allow_unconfirmed_inputs,
                unspent_tx_hash,
                custom_inputs,
                fee_per_kb,
                estimate_fee_per_kb,
                estimate_fee_per_kb_nblocks,
                exact_fee,
                size_for_fee,
                fee_provided,
                destination_btc_out,
                data_btc_out,
                regular_dust_size,
                disable_utxo_locks,
            )
            btc_in = n_btc_in
            final_fee = n_final_fee
        else:
            # when encoding is P2SH and the pretx txid is passed we can skip coinselection
            inputs, change_quantity = None, None

        """Finish"""

        if change_quantity:
            change_output = (source_address, change_quantity)
        else:
            change_output = None

        unsigned_pretx_hex = None
        unsigned_tx_hex = None

        pretx_txid = None
        if encoding == "p2sh":
            assert not (segwit and p2sh_pretx_txid)  # shouldn't do old style with segwit enabled

            if p2sh_pretx_txid:
                pretx_txid = (
                    p2sh_pretx_txid
                    if isinstance(p2sh_pretx_txid, bytes)
                    else binascii.unhexlify(p2sh_pretx_txid)
                )
                unsigned_pretx = None
            else:
                destination_value_sum = sum([value for (destination, value) in destination_outputs])
                source_value = destination_value_sum

                if change_output:
                    # add the difference between source and destination to the change
                    change_value = change_output[1] + (destination_value_sum - source_value)
                    change_output = (change_output[0], change_value)

                unsigned_pretx = serializer.serialise_p2sh_pretx(
                    inputs,
                    source=source_address,
                    source_value=source_value,
                    data_output=data_output,
                    change_output=change_output,
                    pubkey=dust_return_pubkey,
                    multisig_pubkeys=p2sh_source_multisig_pubkeys,
                    multisig_pubkeys_required=p2sh_source_multisig_pubkeys_required,
                )
                unsigned_pretx_hex = binascii.hexlify(unsigned_pretx).decode("utf-8")

            # with segwit we already know the txid and can return both
            if segwit:
                # pretx_txid = hashlib.sha256(unsigned_pretx).digest()  # this should be segwit txid
                ptx = CTransaction.stream_deserialize(
                    io.BytesIO(unsigned_pretx)
                )  # could be a non-segwit tx anyways
                txid_ba = bytearray(ptx.GetTxid())
                txid_ba.reverse()
                pretx_txid = bytes(txid_ba)  # gonna leave the malleability problem to upstream
                self.logger.debug(f"pretx_txid {pretx_txid}")
                print("pretx txid:", binascii.hexlify(pretx_txid))

            if unsigned_pretx:
                # we set a long lock on this, don't want other TXs to spend from it
                self.utxo_p2sh_encoding_locks.set(make_outkey_vin(unsigned_pretx, 0), True)

            # only generate the data TX if we have the pretx txId
            if pretx_txid:
                source_input = None
                if script.is_p2sh(source):
                    source_input = self.select_any_coin_from_source(source)
                    if not source_input:
                        raise exceptions.TransactionError(
                            "Unable to select source input for p2sh source address"
                        )

                unsigned_datatx = serializer.serialise_p2sh_datatx(
                    pretx_txid,
                    source=source_address,
                    source_input=source_input,
                    destination_outputs=destination_outputs,
                    data_output=data_output,
                    pubkey=dust_return_pubkey,
                    multisig_pubkeys=p2sh_source_multisig_pubkeys,
                    multisig_pubkeys_required=p2sh_source_multisig_pubkeys_required,
                )
                unsigned_datatx_hex = binascii.hexlify(unsigned_datatx).decode("utf-8")

                # let the rest of the code work it's magic on the data tx
                unsigned_tx_hex = unsigned_datatx_hex
            else:
                # we're just gonna return the pretx, it doesn't require any of the further checks
                self.logger.warning(f"old_style_api = {old_style_api}")
                return return_result([unsigned_pretx_hex], old_style_api=old_style_api)

        else:
            # Serialise inputs and outputs.
            unsigned_tx = serializer.serialise(
                encoding,
                inputs,
                destination_outputs,
                data_output,
                change_output,
                dust_return_pubkey=dust_return_pubkey,
            )
            unsigned_tx_hex = binascii.hexlify(unsigned_tx).decode("utf-8")

        """Sanity Check"""

        # Desired transaction info.
        (desired_source, desired_destination_outputs, desired_data) = tx_info
        desired_source = script.make_canonical(desired_source)
        desired_destination = (
            script.make_canonical(desired_destination_outputs[0][0])
            if desired_destination_outputs
            else ""
        )
        # NOTE: Include change in destinations for BTC transactions.
        # if change_output and not desired_data and desired_destination != config.UNSPENDABLE:
        #    if desired_destination == '':
        #        desired_destination = desired_source
        #    else:
        #        desired_destination += f'-{desired_source}'
        # NOTE
        if desired_data == None:  # noqa: E711
            desired_data = b""

        # Parsed transaction info.
        try:
            if pretx_txid and unsigned_pretx:
                self.backend.cache_pretx(pretx_txid, unsigned_pretx)
            parsed_source, parsed_destination, x, y, parsed_data, extra = (
                # TODO: inject
                gettxinfo.get_tx_info_new(
                    db,
                    BlockchainParser().deserialize_tx(unsigned_tx_hex),
                    ledger.CURRENT_BLOCK_INDEX,
                    p2sh_is_segwit=script.is_bech32(desired_source),
                    composing=True,
                )
            )

            if encoding == "p2sh":
                # make_canonical can't determine the address, so we blindly change the desired to the parsed
                desired_source = parsed_source

            if pretx_txid and unsigned_pretx:
                self.backend.clear_pretx(pretx_txid)
        except exceptions.BTCOnlyError:
            # Skip BTC‐only transactions.
            if extended_tx_info:
                return {
                    "btc_in": btc_in,
                    "btc_out": destination_btc_out + data_btc_out,
                    "btc_change": change_quantity,
                    "btc_fee": final_fee,
                    "tx_hex": unsigned_tx_hex,
                }
            self.logger.debug("BTC-ONLY")
            return return_result([unsigned_pretx_hex, unsigned_tx_hex], old_style_api=old_style_api)
        desired_source = script.make_canonical(desired_source)

        # Check desired info against parsed info.
        desired = (desired_source, desired_destination, desired_data)
        parsed = (parsed_source, parsed_destination, parsed_data)
        if desired != parsed:
            # Unlock (revert) UTXO locks
            if self.utxo_locks is not None and inputs:
                for input in inputs:
                    self.utxo_locks[source].pop(make_outkey(input), None)

            raise exceptions.TransactionError(
                f"Constructed transaction does not parse correctly: {desired} ≠ {parsed}"
            )

        if extended_tx_info:
            return {
                "btc_in": btc_in,
                "btc_out": destination_btc_out + data_btc_out,
                "btc_change": change_quantity,
                "btc_fee": final_fee,
                "tx_hex": unsigned_tx_hex,
            }
        return return_result([unsigned_pretx_hex, unsigned_tx_hex], old_style_api=old_style_api)


# UTILS


def print_coin(coin):
    return f"amount: {coin['amount']:.8f}; txid: {coin['txid']}; vout: {coin['vout']}; confirmations: {coin.get('confirmations', '?')}"  # simplify and make deterministic


def chunks(l, n):  # noqa: E741
    """Yield successive n‐sized chunks from l."""
    for i in range(0, len(l), n):
        yield l[i : i + n]


def make_outkey(output):
    return f"{output['txid']}{output['vout']}"


def make_outkey_vin(txhex, vout):
    txbin = binascii.unhexlify(txhex) if isinstance(txhex, str) else txhex
    assert isinstance(vout, int)

    tx = bitcoinlib.core.CTransaction.deserialize(txbin)
    outkey = [(vin.prevout.hash, vin.prevout.n) for vin in tx.vin]
    outkey = hashlib.sha256((f"{outkey}{vout}").encode("ascii")).digest()

    return outkey


def return_result(tx_hexes, old_style_api):
    tx_hexes = list(filter(None, tx_hexes))  # filter out None

    if old_style_api:
        if len(tx_hexes) != 1:
            raise Exception("Can't do 2 TXs with old_style_api")

        return tx_hexes[0]
    else:
        if len(tx_hexes) == 1:
            return tx_hexes[0]
        else:
            return tx_hexes


# TODO: this should be lifted
def get_dust_return_pubkey(source, provided_pubkeys, encoding):
    """Return the pubkey to which dust from data outputs will be sent.

    This pubkey is used in multi-sig data outputs (as the only real pubkey) to
    make those the outputs spendable. It is derived from the source address, so
    that the dust is spendable by the creator of the transaction.
    """
    # Get hex dust return pubkey.
    if script.is_multisig(source):
        a, self_pubkeys, b = script.extract_array(
            backend.multisig_pubkeyhashes_to_pubkeys(source, provided_pubkeys)
        )
        dust_return_pubkey_hex = self_pubkeys[0]
    else:
        dust_return_pubkey_hex = backend.pubkeyhash_to_pubkey(source, provided_pubkeys)

    # Convert hex public key into the (binary) dust return pubkey.
    try:
        dust_return_pubkey = binascii.unhexlify(dust_return_pubkey_hex)
    except binascii.Error:
        raise script.InputError("Invalid private key.")  # noqa: B904

    return dust_return_pubkey