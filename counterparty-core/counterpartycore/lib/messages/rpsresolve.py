#! /usr/bin/python3

import binascii
import logging
import string
import struct

from counterpartycore.lib import config, database, exceptions, ledger, message_type, util

from . import rps

logger = logging.getLogger(config.LOGGER_NAME)

# move random rps_match_id
FORMAT = ">H16s32s32s"
LENGTH = 2 + 16 + 32 + 32
ID = 81


def initialise(db):
    cursor = db.cursor()

    # remove misnamed indexes
    database.drop_indexes(cursor, ["block_index_idx", "source_idx", "rps_match_id_idx"])

    cursor.execute("""CREATE TABLE IF NOT EXISTS rpsresolves(
                      tx_index INTEGER PRIMARY KEY,
                      tx_hash TEXT UNIQUE,
                      block_index INTEGER,
                      source TEXT,
                      move INTEGER,
                      random TEXT,
                      rps_match_id TEXT,
                      status TEXT,
                      FOREIGN KEY (tx_index, tx_hash, block_index) REFERENCES transactions(tx_index, tx_hash, block_index))
                   """)

    database.create_indexes(
        cursor,
        "rpsresolves",
        [
            ["block_index"],
            ["source"],
            ["rps_match_id"],
        ],
    )


def validate(db, source, move, random, rps_match_id):
    problems = []
    rps_match = None

    if not isinstance(move, int):
        problems.append("move must be a integer")
        return None, None, problems

    if not all(c in string.hexdigits for c in random):
        problems.append("random must be an hexadecimal string")
        return None, None, problems

    random_bytes = binascii.unhexlify(random)
    if len(random_bytes) != 16:
        problems.append("random must be 16 bytes in hexadecimal format")
        return None, None, problems

    rps_matches = ledger.get_rps_match(db, id=rps_match_id)
    if len(rps_matches) == 0:
        problems.append("no such rps match")
        return None, rps_match, problems
    elif len(rps_matches) > 1:
        assert False  # noqa: B011

    rps_match = rps_matches[0]

    if move < 1:
        problems.append("move must be greater than 0")
    elif move > rps_match["possible_moves"]:
        problems.append(f"move must be lower than {rps_match['possible_moves']}")

    if source not in [rps_match["tx0_address"], rps_match["tx1_address"]]:
        problems.append("invalid source address")
        return None, rps_match, problems

    if rps_match["tx0_address"] == source:
        txn = 0
        rps_match_status = ["pending", "pending and resolved"]
    else:
        txn = 1
        rps_match_status = ["pending", "resolved and pending"]

    move_random_hash = util.dhash(random_bytes + int(move).to_bytes(2, byteorder="big"))
    move_random_hash = binascii.hexlify(move_random_hash).decode("utf-8")
    if rps_match[f"tx{txn}_move_random_hash"] != move_random_hash:
        problems.append("invalid move or random value")
        return txn, rps_match, problems

    if rps_match["status"] == "expired":
        problems.append("rps match expired")
    elif rps_match["status"].startswith("concluded"):
        problems.append("rps match concluded")
    elif rps_match["status"].startswith("invalid"):
        problems.append("rps match invalid")
    elif rps_match["status"] not in rps_match_status:
        problems.append("rps already resolved")

    return txn, rps_match, problems


def compose(db, source: str, move: int, random: str, rps_match_id: str):
    tx0_hash, tx1_hash = util.parse_id(rps_match_id)

    txn, rps_match, problems = validate(db, source, move, random, rps_match_id)
    if problems:
        raise exceptions.ComposeError(problems)

    # Warn if down to the wire.
    time_left = rps_match["match_expire_index"] - util.CURRENT_BLOCK_INDEX
    if time_left < 4:
        logger.warning(
            f"Only {time_left} blocks until that rps match expires. The conclusion might not make into the blockchain in time."
        )

    tx0_hash_bytes = binascii.unhexlify(bytes(tx0_hash, "utf-8"))
    tx1_hash_bytes = binascii.unhexlify(bytes(tx1_hash, "utf-8"))
    random_bytes = binascii.unhexlify(bytes(random, "utf-8"))
    data = message_type.pack(ID)
    data += struct.pack(FORMAT, move, random_bytes, tx0_hash_bytes, tx1_hash_bytes)
    return (source, [], data)


def unpack(message, return_dict=False):
    try:
        if len(message) != LENGTH:
            raise exceptions.UnpackError
        move, random, tx0_hash_bytes, tx1_hash_bytes = struct.unpack(FORMAT, message)
        tx0_hash, tx1_hash = (
            binascii.hexlify(tx0_hash_bytes).decode("utf-8"),
            binascii.hexlify(tx1_hash_bytes).decode("utf-8"),
        )
        rps_match_id = util.make_id(tx0_hash, tx1_hash)
        random = binascii.hexlify(random).decode("utf-8")
        status = "valid"
    except (exceptions.UnpackError, struct.error) as e:  # noqa: F841
        move, random, tx0_hash, tx1_hash, rps_match_id = None, None, None, None, None
        status = "invalid: could not unpack"

    if return_dict:
        return {
            "move": move,
            "random": random,
            "rps_match_id": rps_match_id,
            "status": status,
        }
    return move, random, rps_match_id, status


def parse(db, tx, message):
    cursor = db.cursor()

    # Unpack message.
    move, random, rps_match_id, status = unpack(message)

    if status == "valid":
        txn, rps_match, problems = validate(db, tx["source"], move, random, rps_match_id)
        if problems:
            rps_match = None
            status = "invalid: " + "; ".join(problems)

    # Add parsed transaction to message-type–specific table.
    rpsresolves_bindings = {
        "tx_index": tx["tx_index"],
        "tx_hash": tx["tx_hash"],
        "block_index": tx["block_index"],
        "source": tx["source"],
        "move": move,
        "random": random,
        "rps_match_id": rps_match_id,
        "status": status,
    }

    if status == "valid":
        rps_match_status = "concluded"

        if rps_match["status"] == "pending":
            rps_match_status = "resolved and pending" if txn == 0 else "pending and resolved"

        if rps_match_status == "concluded":
            counter_txn = 0 if txn == 1 else 1
            counter_source = rps_match[f"tx{counter_txn}_address"]
            counter_games = ledger.get_rpsresolves(
                db, source=counter_source, status="valid", rps_match_id=rps_match_id
            )
            assert len(counter_games) == 1
            counter_game = counter_games[0]

            winner = resolve_game(db, rpsresolves_bindings, counter_game)

            if winner == 0:
                rps_match_status = "concluded: tie"
            elif winner == counter_game["tx_index"]:
                rps_match_status = (
                    f"concluded: {'first' if counter_txn == 0 else 'second'} player wins"
                )
            else:
                rps_match_status = f"concluded: {'first' if txn == 0 else 'second'} player wins"

        rps.update_rps_match_status(
            db, rps_match, rps_match_status, tx["block_index"], tx["tx_index"]
        )

    ledger.insert_record(db, "rpsresolves", rpsresolves_bindings, "RPS_RESOLVE")

    cursor.close()


# https://en.wikipedia.org/wiki/Rock-paper-scissors#Additional_weapons:
def resolve_game(db, resovlerps1, resovlerps2):
    move1 = resovlerps1["move"]
    move2 = resovlerps2["move"]

    same_parity = (move1 % 2) == (move2 % 2)
    if (same_parity and move1 < move2) or (not same_parity and move1 > move2):
        return resovlerps1["tx_index"]
    elif (same_parity and move1 > move2) or (not same_parity and move1 < move2):
        return resovlerps2["tx_index"]
    else:
        return 0


# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4
