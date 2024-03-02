import binascii
import logging
import struct

import bitcoin as bitcoinlib
from bitcoin.core.script import CScriptInvalidError

from counterpartylib.lib import ledger, script, config, backend, arc4
from counterpartylib.lib.exceptions import DecodeError, BTCOnlyError
from counterpartylib.lib.kickstart.utils import ib2h
from counterpartylib.lib.kickstart.blocks_parser import BlockchainParser
from counterpartylib.lib.transaction_helper import p2sh_encoding
from counterpartylib.lib.messages import dispenser
from counterpartylib.lib.opcodes import *


logger = logging.getLogger(__name__)


def arc4_decrypt(cyphertext, decoded_tx):
    '''Un‐obfuscate. Initialise key once per attempt.'''
    key = arc4.init_arc4(decoded_tx["vin"][0]["hash"][::-1])
    return key.decrypt(cyphertext)


def get_opreturn(asm):
    if len(asm) == 2 and asm[0] == OP_RETURN:
        pubkeyhash = asm[1]
        if type(pubkeyhash) == bytes:
            return pubkeyhash
    raise DecodeError('invalid OP_RETURN')


def decode_opreturn(asm, decoded_tx):
    chunk = get_opreturn(asm)
    chunk = arc4_decrypt(chunk, decoded_tx)
    if chunk[:len(config.PREFIX)] == config.PREFIX:             # Data
        destination, data = None, chunk[len(config.PREFIX):]
    else:
        raise DecodeError('unrecognised OP_RETURN output')

    return destination, data


def decode_checksig(asm, decoded_tx):
    pubkeyhash = script.get_checksig(asm)
    chunk = arc4_decrypt(pubkeyhash, decoded_tx)   # TODO: This is slow!
    if chunk[1:len(config.PREFIX) + 1] == config.PREFIX:        # Data
        # Padding byte in each output (instead of just in the last one) so that encoding methods may be mixed. Also, it’s just not very much data.
        chunk_length = chunk[0]
        chunk = chunk[1:chunk_length + 1]
        destination, data = None, chunk[len(config.PREFIX):]
    else:                                                       # Destination
        pubkeyhash = binascii.hexlify(pubkeyhash).decode('utf-8')
        destination, data = script.base58_check_encode(pubkeyhash, config.ADDRESSVERSION), None
    return destination, data


def decode_scripthash(asm):
    destination = script.base58_check_encode(binascii.hexlify(asm[1]).decode('utf-8'), config.P2SH_ADDRESSVERSION)

    return destination, None


def decode_checkmultisig(asm, decoded_tx):
    pubkeys, signatures_required = script.get_checkmultisig(asm)
    chunk = b''
    for pubkey in pubkeys[:-1]:     # (No data in last pubkey.)
        chunk += pubkey[1:-1]       # Skip sign byte and nonce byte.
    chunk = arc4_decrypt(chunk, decoded_tx)
    if chunk[1:len(config.PREFIX) + 1] == config.PREFIX:        # Data
        # Padding byte in each output (instead of just in the last one) so that encoding methods may be mixed. Also, it’s just not very much data.
        chunk_length = chunk[0]
        chunk = chunk[1:chunk_length + 1]
        destination, data = None, chunk[len(config.PREFIX):]
    else:                                                       # Destination
        pubkeyhashes = [script.pubkey_to_pubkeyhash(pubkey) for pubkey in pubkeys]
        destination, data = script.construct_array(signatures_required, pubkeyhashes, len(pubkeyhashes)), None

    return destination, data


def get_pubkeyhash(scriptpubkey, block_index):
    asm = script.script_to_asm(scriptpubkey)
    if ledger.enabled('multisig_addresses', block_index=block_index):
        if len(asm) > 0:

            if asm[0] == OP_DUP:
                if len(asm) != 5 or asm[1] != OP_HASH160 or asm[3] != OP_EQUALVERIFY or asm[4] != OP_CHECKSIG:            
                    return None, None
                else:
                    return asm[2], config.ADDRESSVERSION

            elif (asm[0] == OP_HASH160) and ledger.enabled('p2sh_dispensers_support'):
                if len(asm) != 3 or asm[-1] != 'OP_EQUAL':          
                    return None, None
                else:
                    return asm[1], config.P2SH_ADDRESSVERSION
        return None, None
    else:
        if len(asm) != 5 or asm[0] != OP_DUP or asm[1] != OP_HASH160 or asm[3] != OP_EQUALVERIFY or asm[4] != OP_CHECKSIG:
            return None, None
        return asm[2], config.ADDRESSVERSION


def is_witness_v0_keyhash(scriptpubkey):
        """Returns true if this is a scriptpubkey for V0 P2WPKH. """
        return len(scriptpubkey) == 22 and scriptpubkey[0:2] == b'\x00\x14'


def get_address(scriptpubkey, block_index):
    if ledger.enabled('correct_segwit_txids') and is_witness_v0_keyhash(scriptpubkey):
        address = script.script_to_address(scriptpubkey)
        return address
    else:
        pubkeyhash, address_version = get_pubkeyhash(scriptpubkey, block_index)
        if not pubkeyhash:
            return False
        pubkeyhash = binascii.hexlify(pubkeyhash).decode('utf-8')
        address = script.base58_check_encode(pubkeyhash, address_version)
        # Test decoding of address.
        if address != config.UNSPENDABLE and binascii.unhexlify(bytes(pubkeyhash, 'utf-8')) != script.base58_check_decode(address, address_version):
            return False
        return address


def get_transaction_sources(decoded_tx, block_parser=None):
    sources = []
    outputs_value = 0

    for vin in decoded_tx["vin"][:]:                   # Loop through inputs.
        scriptPubKey = None
        if block_parser:
            vin_ctx = block_parser.read_raw_transaction(ib2h(vin["hash"]))
        else:
            # Note: We don't know what block the `vin` is in, and the block might have been from a while ago, so this call may not hit the cache.
            vin_tx = backend.getrawtransaction(ib2h(vin["hash"]), block_index=None)
            vin_ctx = BlockchainParser().deserialize_tx(vin_tx)

        vout = vin_ctx["vout"][vin["n"]]
        outputs_value += vout["nValue"]
        scriptPubKey = vout["scriptPubKey"]

        asm = script.script_to_asm(scriptPubKey)

        if asm[-1] == OP_CHECKSIG:
            new_source, new_data = decode_checksig(asm, decoded_tx)
            if new_data or not new_source:
                raise DecodeError('data in source')
        elif asm[-1] == OP_CHECKMULTISIG:
            new_source, new_data = decode_checkmultisig(asm, decoded_tx)
            if new_data or not new_source:
                raise DecodeError('data in source')
        elif asm[0] == OP_HASH160 and asm[-1] == OP_EQUAL and len(asm) == 3:
            new_source, new_data = decode_scripthash(asm)
            if new_data or not new_source:
                raise DecodeError('data in source')
        elif ledger.enabled('segwit_support') and asm[0] == b'':
            # Segwit output
            new_source = script.script_to_address(scriptPubKey)
            new_data = None
        else:
            raise DecodeError('unrecognised source type')

        # old; append to sources, results in invalid addresses
        # new; first found source is source, the rest can be anything (to fund the TX for example)
        if not (ledger.enabled('first_input_is_source') and len(sources)):
            # Collect unique sources.
            if new_source not in sources:
                sources.append(new_source)

    return '-'.join(sources), outputs_value


def get_transaction_source_from_p2sh(decoded_tx, p2sh_is_segwit, block_parser=None):
    p2sh_encoding_source = None
    data = b''
    outputs_value = 0

    for vin in decoded_tx["vin"]:
        if block_parser:
            vin_ctx = block_parser.read_raw_transaction(ib2h(vin["hash"]))
        else:
            # Note: We don't know what block the `vin` is in, and the block might have been from a while ago, so this call may not hit the cache.
            vin_tx = backend.getrawtransaction(ib2h(vin["hash"]), block_index=None)
            vin_ctx = BlockchainParser().deserialize_tx(vin_tx)

        if ledger.enabled("prevout_segwit_fix"):
            prevout_is_segwit = len(vin_ctx['vtxinwit']) > 0
        else:
            prevout_is_segwit = p2sh_is_segwit
        
        vout = vin_ctx["vout"][vin["n"]]
        outputs_value += vout["nValue"]

        # Ignore transactions with invalid script.
        asm = script.script_to_asm(vin["scriptSig"])

        new_source, new_destination, new_data = p2sh_encoding.decode_p2sh_input(asm, p2sh_is_segwit=prevout_is_segwit)
        # this could be a p2sh source address with no encoded data
        if new_data is None:
            continue;

        if new_source is not None:
            if p2sh_encoding_source is not None and new_source != p2sh_encoding_source:
                # this p2sh data input has a bad source address
                raise DecodeError('inconsistent p2sh inputs')

            p2sh_encoding_source = new_source

        assert not new_destination

        data += new_data

    return p2sh_encoding_source, data, outputs_value


def get_dispensers_outputs(db, potential_dispensers):
    outputs = []
    for (destination, btc_amount) in potential_dispensers:
        if destination is None or btc_amount is None:
            continue
        if dispenser.is_dispensable(db, destination, btc_amount):
            outputs.append((destination, btc_amount))
    return outputs


def get_dispensers_tx_info(sources, dispensers_outputs):
    source, destination, btc_amount, fee, data, outs = b'', None, None, None, None, []

    dispenser_source = sources.split("-")[0]
    out_index = 0
    for out in dispensers_outputs:
        if out[0] != dispenser_source:
            source = dispenser_source
            destination = out[0]
            btc_amount = out[1]
            fee = 0
            data = struct.pack(config.SHORT_TXTYPE_FORMAT, dispenser.DISPENSE_ID)
            data += b'\x00'

            if ledger.enabled("multiple_dispenses"):
                outs.append({"destination":out[0], "btc_amount":out[1], "out_index":out_index})
            else:
                break # Prevent inspection of further dispenses (only first one is valid)

        out_index = out_index + 1

    return source, destination, btc_amount, fee, data, outs


def parse_transaction_vouts(decoded_tx, p2sh_support):
    # Get destinations and data outputs.
    destinations, btc_amount, fee, data, potential_dispensers = [], 0, 0, b'', []

    for vout in decoded_tx["vout"]:
        potential_dispensers.append((None, None))
        # Fee is the input values minus output values.
        output_value = vout["nValue"]
        fee -= output_value

        # Ignore transactions with invalid script.
        asm = script.script_to_asm(vout["scriptPubKey"])
        if asm[0] == OP_RETURN:
            new_destination, new_data = decode_opreturn(asm, decoded_tx)
        elif asm[-1] == OP_CHECKSIG:
            new_destination, new_data = decode_checksig(asm, decoded_tx)
            potential_dispensers[-1] = (new_destination, output_value)
        elif asm[-1] == OP_CHECKMULTISIG:
            try:
                new_destination, new_data = decode_checkmultisig(asm, decoded_tx)
                potential_dispensers[-1] = (new_destination, output_value)
            except:
                raise DecodeError('unrecognised output type')
        elif p2sh_support and asm[0] == OP_HASH160 and asm[-1] == OP_EQUAL and len(asm) == 3:
            new_destination, new_data = decode_scripthash(asm)
            if ledger.enabled('p2sh_dispensers_support'):
                potential_dispensers[-1] = (new_destination, output_value)
        elif ledger.enabled('segwit_support') and asm[0] == b'':
            # Segwit Vout, second param is redeemScript
            #redeemScript = asm[1]
            new_destination = script.script_to_address(vout["scriptPubKey"])
            new_data = None
            if ledger.enabled('correct_segwit_txids'):
                potential_dispensers[-1] = (new_destination, output_value)
        else:
            raise DecodeError('unrecognised output type')
        assert not (new_destination and new_data)
        assert new_destination != None or new_data != None  # `decode_*()` should never return `None, None`.

        if ledger.enabled('null_data_check'):
            if new_data == []:
                raise DecodeError('new destination is `None`')

        # All destinations come before all data.
        if not data and not new_data and destinations != [config.UNSPENDABLE,]:
            destinations.append(new_destination)
            btc_amount += output_value
        else:
            if new_destination:     # Change.
                break
            else:                   # Data.
                data += new_data

    return destinations, btc_amount, fee, data, potential_dispensers


def get_tx_info_new(db, decoded_tx, block_index, block_parser=None, p2sh_support=False, p2sh_is_segwit=False):
    """Get multisig transaction info.
    The destinations, if they exists, always comes before the data output; the
    change, if it exists, always comes after.
    """

    # Ignore coinbase transactions.
    if decoded_tx['coinbase']:
        raise DecodeError('coinbase transaction')

    # Get destinations and data outputs.
    destinations, btc_amount, fee, data, potential_dispensers = parse_transaction_vouts(decoded_tx, p2sh_support)

    # source can be determined by parsing the p2sh_data transaction
    #   or from the first spent output
    sources = []
    fee_added = False
    # P2SH encoding signalling
    p2sh_encoding_source = None
    if ledger.enabled('p2sh_encoding') and data == b'P2SH':
        p2sh_encoding_source, data, outputs_value = get_transaction_source_from_p2sh(
            decoded_tx, p2sh_is_segwit, block_parser=block_parser
        )
        fee += outputs_value
        fee_added = True

    # Only look for source if data were found or destination is `UNSPENDABLE`,
    # for speed.
    dispensers_outputs = []
    if not data and destinations != [config.UNSPENDABLE,]:
        if ledger.enabled('dispensers', block_index):
            dispensers_outputs = get_dispensers_outputs(db, potential_dispensers)
            if len(dispensers_outputs) == 0:
                raise BTCOnlyError('no data and not unspendable')
        else:
            raise BTCOnlyError('no data and not unspendable')

    # Collect all (unique) source addresses.
    #   if we haven't found them yet
    if p2sh_encoding_source is None:
        sources, outputs_value = get_transaction_sources(decoded_tx, block_parser=block_parser)
        if not fee_added:
            fee += outputs_value
    else: # use the source from the p2sh data source
        sources = p2sh_encoding_source

    if not data and destinations != [config.UNSPENDABLE,]:
        assert ledger.enabled('dispensers', block_index) # else an exception would have been raised above
        assert len(dispensers_outputs) > 0 # else an exception would have been raised above
        return get_dispensers_tx_info(sources, dispensers_outputs)

    destinations = '-'.join(destinations)
    return sources, destinations, btc_amount, round(fee), data, []


def get_tx_info_legacy(decoded_tx, block_index, block_parser=None):
    """Get singlesig transaction info.
    The destination, if it exists, always comes before the data output; the
    change, if it exists, always comes after.
    """

    # Fee is the input values minus output values.
    fee = 0

    # Get destination output and data output.
    destination, btc_amount, data = None, None, b''
    pubkeyhash_encoding = False
    for vout in decoded_tx["vout"]:
        fee -= vout["nValue"]

        # Sum data chunks to get data. (Can mix OP_RETURN and multi-sig.)
        asm = script.script_to_asm(vout["scriptPubKey"])
        if len(asm) == 2 and asm[0] == OP_RETURN:                                             # OP_RETURN
            if type(asm[1]) != bytes:
                continue
            data_chunk = asm[1]
            data += data_chunk
        elif len(asm) == 5 and asm[0] == 1 and asm[3] == 2 and asm[4] == OP_CHECKMULTISIG:    # Multi-sig
            if type(asm[2]) != bytes:
                continue
            data_pubkey = asm[2]
            data_chunk_length = data_pubkey[0]  # No ord() necessary.
            data_chunk = data_pubkey[1:data_chunk_length + 1]
            data += data_chunk
        elif len(asm) == 5 and (block_index >= 293000 or config.TESTNET or config.REGTEST):    # Protocol change.
            # Be strict.
            pubkeyhash, address_version = get_pubkeyhash(vout["scriptPubKey"], block_index)
            if not pubkeyhash:
                continue

            if decoded_tx["coinbase"]:
                raise DecodeError('coinbase transaction')

            data_pubkey = arc4_decrypt(pubkeyhash, decoded_tx)
            if data_pubkey[1:9] == config.PREFIX or pubkeyhash_encoding:
                pubkeyhash_encoding = True
                data_chunk_length = data_pubkey[0]  # No ord() necessary.
                data_chunk = data_pubkey[1:data_chunk_length + 1]
                if data_chunk[-8:] == config.PREFIX:
                    data += data_chunk[:-8]
                    break
                else:
                    data += data_chunk

        # Destination is the first output before the data.
        if not destination and not btc_amount and not data:
            address = get_address(vout["scriptPubKey"], block_index)
            if address:
                destination = address
                btc_amount = vout["nValue"]

    # Check for, and strip away, prefix (except for burns).
    if destination == config.UNSPENDABLE:
        pass
    elif data[:len(config.PREFIX)] == config.PREFIX:
        data = data[len(config.PREFIX):]
    else:
        raise DecodeError('no prefix')

    # Only look for source if data were found or destination is UNSPENDABLE, for speed.
    if not data and destination != config.UNSPENDABLE:
        raise BTCOnlyError('no data and not unspendable')

    # Collect all possible source addresses; ignore coinbase transactions and anything but the simplest Pay‐to‐PubkeyHash inputs.
    source_list = []
    for vin in decoded_tx["vin"][:]:                                               # Loop through input transactions.

        if vin["coinbase"]:
            raise DecodeError('coinbase transaction')

         # Get the full transaction data for this input transaction.
        scriptPubKey = None
        if block_parser:
            vin_ctx = block_parser.read_raw_transaction(ib2h(vin["hash"]))
        else:
            # Note: We don't know what block the `vin` is in, and the block might have been from a while ago, so this call may not hit the cache.
            vin_tx = backend.getrawtransaction(ib2h(vin["hash"]), block_index=None)
            vin_ctx = BlockchainParser().deserialize_tx(vin_tx)

        vout = vin_ctx["vout"][vin["n"]]
        fee += vout["nValue"]
        scriptPubKey = vout["scriptPubKey"]

        address = get_address(scriptPubKey, block_index)
        if not address:
            raise DecodeError('invalid scriptpubkey')
        else:
            source_list.append(address)

    # Require that all possible source addresses be the same.
    if all(x == source_list[0] for x in source_list):
        source = source_list[0]
    else:
        source = None

    return source, destination, btc_amount, fee, data, []


def _get_tx_info(db, decoded_tx, block_index, block_parser=None, p2sh_is_segwit=False):
    """Get the transaction info. Calls one of two subfunctions depending on signature type."""
    if not block_index:
        block_index = ledger.CURRENT_BLOCK_INDEX

    if ledger.enabled('p2sh_addresses', block_index=block_index):   # Protocol change.
        return get_tx_info_new(
            db,
            decoded_tx,
            block_index,
            block_parser=block_parser,
            p2sh_support=True,
            p2sh_is_segwit=p2sh_is_segwit,
        )
    elif ledger.enabled('multisig_addresses', block_index=block_index):   # Protocol change.
        return get_tx_info_new(
            db,
            decoded_tx,
            block_index,
            block_parser=block_parser,
        )
    else:
        return get_tx_info_legacy(
            decoded_tx,
            block_index,
            block_parser=block_parser
        )


def get_tx_info(db, decoded_tx, block_index, block_parser=None):
    """Get the transaction info. Returns normalized None data for DecodeError and BTCOnlyError."""
    try:
        return _get_tx_info(db, decoded_tx, block_index, block_parser)
    except DecodeError as e:
        return b'', None, None, None, None, None
    except BTCOnlyError as e:
        return b'', None, None, None, None, None