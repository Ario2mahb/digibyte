#!/usr/bin/env python3
# Copyright (c) 2014-2019 The DigiByte Core developers
# Copyright (c) 2022 The DigiByte Core developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.
"""Test the wallet."""
from decimal import Decimal
from itertools import product
from time import sleep

from test_framework.blocktools import COINBASE_MATURITY_2
from test_framework.test_framework import DigiByteTestFramework
from test_framework.util import (
    assert_array_result,
    assert_equal,
    assert_fee_amount,
    assert_raises_rpc_error,
)
from test_framework.wallet_util import test_address

OUT_OF_RANGE = "Amount out of range"


class WalletTest(DigiByteTestFramework):
    def set_test_params(self):
        self.num_nodes = 4
        self.extra_args = [[
            "-acceptnonstdtxn=1",
            "-dandelion=0",
        ]] * self.num_nodes

        self.setup_clean_chain = True
        self.supports_cli = False

    def skip_test_if_missing_module(self):
        self.skip_if_no_wallet()

    def setup_network(self):
        self.setup_nodes()
        # Only need nodes 0-2 running at start of test
        self.stop_node(3)
        self.connect_nodes(0, 1)
        self.connect_nodes(1, 2)
        self.connect_nodes(0, 2)
        self.sync_all(self.nodes[0:3])

    def check_fee_amount(self, curr_balance, balance_with_fee, fee_per_byte, tx_size):
        """Return curr_balance after asserting the fee was in range"""
        fee = balance_with_fee - curr_balance
        assert_fee_amount(fee, tx_size, fee_per_byte * 1000)
        return curr_balance

    def get_vsize(self, txn):
        return self.nodes[0].decoderawtransaction(txn)['vsize']


    # Get newly matured utxo with min_amount
    # DigiByte specific: COINBASE_MATURITY is set to 8 instead of 100 (in BTC).
    # During this test suite, more and more coinbase utxos are maturing.
    # This helper function collects them.
    def get_matured_utxos(self, node_index, utxo_seen_map = None, allow_none_coinbase = False, min_amount = Decimal('72000')):
        if node_index is None: raise ValueError('node_index not specified')
        if utxo_seen_map is None: raise ValueError('utxo_seen_map not specified')

        new_coinbase_txs = []

        unspent = self.nodes[node_index].listunspent(query_options={'minimumAmount': min_amount})

        for utxo in unspent:
            if allow_none_coinbase == False:
                if not 'label' in utxo: continue
                if utxo['label'] != 'coinbase': continue

            if utxo['txid'] in utxo_seen_map: continue
            new_coinbase_txs.append(utxo)
            utxo_seen_map[utxo['txid']] = True

        return new_coinbase_txs


    def run_test(self):
        utxo_seen = [{}] * self.num_nodes

        # Check that there's no UTXO on none of the nodes
        assert_equal(len(self.nodes[0].listunspent()), 0)
        assert_equal(len(self.nodes[1].listunspent()), 0)
        assert_equal(len(self.nodes[2].listunspent()), 0)

        self.log.info("Mining blocks...")

        self.generate(self.nodes[0], 1, sync_fun=self.no_op)

        walletinfo = self.nodes[0].getwalletinfo()
        assert_equal(walletinfo['immature_balance'], 72000)
        assert_equal(walletinfo['balance'], 0)

        self.sync_all(self.nodes[0:3])
        self.generate(self.nodes[1], COINBASE_MATURITY_2 + 1, sync_fun=lambda: self.sync_all(self.nodes[0:3]))

        utxo_count = 1
        expected_balance = 72000 * utxo_count
        assert_equal(self.nodes[0].getbalance(), 72000)
        assert_equal(self.nodes[1].getbalance(), expected_balance)
        assert_equal(self.nodes[2].getbalance(), 0)

        # Check that only first and second nodes have UTXOs
        utxos = self.nodes[0].listunspent()
        assert_equal(len(utxos), 1)
        assert_equal(len(self.nodes[1].listunspent()), utxo_count)
        assert_equal(len(self.nodes[2].listunspent()), 0)

        self.log.info("Test gettxout")
        confirmed_txid, confirmed_index = utxos[0]["txid"], utxos[0]["vout"]
        # First, outputs that are unspent both in the chain and in the
        # mempool should appear with or without include_mempool
        txout = self.nodes[0].gettxout(txid=confirmed_txid, n=confirmed_index, include_mempool=False)
        assert_equal(txout['value'], 72000)
        txout = self.nodes[0].gettxout(txid=confirmed_txid, n=confirmed_index, include_mempool=True)
        assert_equal(txout['value'], 72000)

        # Send 21 DGB from 0 to 2 using sendtoaddress call.
        self.nodes[0].sendtoaddress(self.nodes[2].getnewaddress(), 11)
        mempool_txid = self.nodes[0].sendtoaddress(self.nodes[2].getnewaddress(), 10)

        self.log.info("Test gettxout (second part)")
        # utxo spent in mempool should be visible if you exclude mempool
        # but invisible if you include mempool
        txout = self.nodes[0].gettxout(confirmed_txid, confirmed_index, False)
        assert_equal(txout['value'], 72000)
        txout = self.nodes[0].gettxout(confirmed_txid, confirmed_index, True)
        assert txout is None
        # new utxo from mempool should be invisible if you exclude mempool
        # but visible if you include mempool
        txout = self.nodes[0].gettxout(mempool_txid, 0, False)
        assert txout is None
        txout1 = self.nodes[0].gettxout(mempool_txid, 0, True)
        txout2 = self.nodes[0].gettxout(mempool_txid, 1, True)
        # note the mempool tx will have randomly assigned indices
        # but 10 will go to node2 and the rest will go to node0
        balance = self.nodes[0].getbalance()
        assert_equal(set([txout1['value'], txout2['value']]), set([10, balance]))
        walletinfo = self.nodes[0].getwalletinfo()
        assert_equal(walletinfo['immature_balance'], 0)

        # Have node0 mine a block, thus it will collect its own fee.
        self.generate(self.nodes[0], 1, sync_fun=lambda: self.sync_all(self.nodes[0:3]))

        # Exercise locking of unspent outputs
        unspent_0 = self.nodes[2].listunspent()[0]
        unspent_0 = {"txid": unspent_0["txid"], "vout": unspent_0["vout"]}
        assert_raises_rpc_error(-8, "Invalid parameter, expected locked output", self.nodes[2].lockunspent, True, [unspent_0])
        self.nodes[2].lockunspent(False, [unspent_0])
        assert_raises_rpc_error(-8, "Invalid parameter, output already locked", self.nodes[2].lockunspent, False, [unspent_0])
        assert_raises_rpc_error(-6, "Insufficient funds", self.nodes[2].sendtoaddress, self.nodes[2].getnewaddress(), 20)
        assert_equal([unspent_0], self.nodes[2].listlockunspent())
        self.nodes[2].lockunspent(True, [unspent_0])
        assert_equal(len(self.nodes[2].listlockunspent()), 0)
        assert_raises_rpc_error(-8, "txid must be of length 64 (not 34, for '0000000000000000000000000000000000')",
                                self.nodes[2].lockunspent, False,
                                [{"txid": "0000000000000000000000000000000000", "vout": 0}])
        assert_raises_rpc_error(-8, "txid must be hexadecimal string (not 'ZZZ0000000000000000000000000000000000000000000000000000000000000')",
                                self.nodes[2].lockunspent, False,
                                [{"txid": "ZZZ0000000000000000000000000000000000000000000000000000000000000", "vout": 0}])
        assert_raises_rpc_error(-8, "Invalid parameter, unknown transaction",
                                self.nodes[2].lockunspent, False,
                                [{"txid": "0000000000000000000000000000000000000000000000000000000000000000", "vout": 0}])
        assert_raises_rpc_error(-8, "Invalid parameter, vout index out of bounds",
                                self.nodes[2].lockunspent, False,
                                [{"txid": unspent_0["txid"], "vout": 999}])

        # The lock on a manually selected output is ignored
        unspent_0 = self.nodes[1].listunspent()[0]
        self.nodes[1].lockunspent(False, [unspent_0])
        tx = self.nodes[1].createrawtransaction([unspent_0], { self.nodes[1].getnewaddress() : 1 })
        self.nodes[1].fundrawtransaction(tx,{"lockUnspents": True})

        # fundrawtransaction can lock an input
        self.nodes[1].lockunspent(True, [unspent_0])
        assert_equal(len(self.nodes[1].listlockunspent()), 0)
        tx = self.nodes[1].fundrawtransaction(tx,{"lockUnspents": True})['hex']
        assert_equal(len(self.nodes[1].listlockunspent()), 1)

        # Send transaction
        tx = self.nodes[1].signrawtransactionwithwallet(tx)["hex"]
        self.nodes[1].sendrawtransaction(tx)
        assert_equal(len(self.nodes[1].listlockunspent()), 0)

        # Have node1 generate 101 blocks (so node0 can recover the fee)
        self.generate(self.nodes[1], COINBASE_MATURITY_2 + 1, sync_fun=lambda: self.sync_all(self.nodes[0:3]))

        # node0 should end up with 100 btc in block rewards plus fees, but
        # minus the 21 plus fees sent to node2
        assert_equal(self.nodes[0].getbalance(), 2 * 72000 - 21)
        assert_equal(self.nodes[2].getbalance(), 21)

        # Node0 should have two unspent outputs.
        # Create a couple of transactions to send them to node2, submit them through
        # node1, and make sure both node0 and node2 pick them up properly:
        node0utxos = self.nodes[0].listunspent(1)
        assert_equal(len(node0utxos), 2)

        # create both transactions
        txns_to_send = []
        expectedNode2Balance = 21
        for utxo in node0utxos:
            inputs = []
            outputs = {}
            inputs.append({"txid": utxo["txid"], "vout": utxo["vout"]})
            expectedNode2Balance += (utxo["amount"] - 3)
            outputs[self.nodes[2].getnewaddress()] = utxo["amount"] - 3
            raw_tx = self.nodes[0].createrawtransaction(inputs, outputs)
            txns_to_send.append(self.nodes[0].signrawtransactionwithwallet(raw_tx))

        # Have node 1 (miner) send the transactions
        self.nodes[1].sendrawtransaction(hexstring=txns_to_send[0]["hex"], maxfeerate=0)
        self.nodes[1].sendrawtransaction(hexstring=txns_to_send[1]["hex"], maxfeerate=0)

        # Have node1 mine a block to confirm transactions:
        self.generate(self.nodes[1], 1, sync_fun=lambda: self.sync_all(self.nodes[0:3]))

        assert_equal(self.nodes[0].getbalance(), 0)
        assert_equal(self.nodes[2].getbalance(), expectedNode2Balance)

        # Verify that a spent output cannot be locked anymore
        spent_0 = {"txid": node0utxos[0]["txid"], "vout": node0utxos[0]["vout"]}
        assert_raises_rpc_error(-8, "Invalid parameter, expected unspent output", self.nodes[0].lockunspent, False, [spent_0])

        # Send 10 DGB normal
        address = self.nodes[0].getnewaddress("test")
        fee_per_byte = Decimal('0.10') / 1000
        self.nodes[2].settxfee(fee_per_byte * 1000)
        txid = self.nodes[2].sendtoaddress(address, 10, "", "", False)
        expectedNode2Balance -= 10
        self.generate(self.nodes[2], 1, sync_fun=lambda: self.sync_all(self.nodes[0:3]))
        node_2_bal = self.check_fee_amount(self.nodes[2].getbalance(), Decimal(expectedNode2Balance), fee_per_byte, self.get_vsize(self.nodes[2].gettransaction(txid)['hex']))
        assert_equal(self.nodes[0].getbalance(), Decimal('10'))

        # Send 10 DGB with subtract fee from amount
        txid = self.nodes[2].sendtoaddress(address, 10, "", "", True)
        self.generate(self.nodes[2], 1, sync_fun=lambda: self.sync_all(self.nodes[0:3]))

        node_2_bal -= Decimal('10')
        assert_equal(self.nodes[2].getbalance(), node_2_bal)
        node_0_bal = self.check_fee_amount(self.nodes[0].getbalance(), Decimal('20'), fee_per_byte, self.get_vsize(self.nodes[2].gettransaction(txid)['hex']))

        self.log.info("Test sendmany")

        # Sendmany 10 DGB
        txid = self.nodes[2].sendmany('', {address: 10}, 0, "", [])
        self.generate(self.nodes[2], 1, sync_fun=lambda: self.sync_all(self.nodes[0:3]))

        node_0_bal += Decimal('10')
        node_2_bal = self.check_fee_amount(self.nodes[2].getbalance(), node_2_bal - Decimal('10'), fee_per_byte, self.get_vsize(self.nodes[2].gettransaction(txid)['hex']))
        assert_equal(self.nodes[0].getbalance(), node_0_bal)

        # Sendmany 10 DGB with subtract fee from amount
        txid = self.nodes[2].sendmany('', {address: 10}, 0, "", [address])
        self.generate(self.nodes[2], 1, sync_fun=lambda: self.sync_all(self.nodes[0:3]))

        node_2_bal -= Decimal('10')
        assert_equal(self.nodes[2].getbalance(), node_2_bal)
        node_0_bal = self.check_fee_amount(self.nodes[0].getbalance(), node_0_bal + Decimal('10'), fee_per_byte, self.get_vsize(self.nodes[2].gettransaction(txid)['hex']))

        self.log.info("Test sendmany with fee_rate param (explicit fee rate in sat/vB)")
        fee_rate_sat_vb = 10000
        fee_rate_btc_kvb = fee_rate_sat_vb * 1e3 / 1e8
        explicit_fee_rate_btc_kvb = Decimal(fee_rate_btc_kvb) / 1000

        # Test passing fee_rate as a string
        txid = self.nodes[2].sendmany(amounts={address: 10}, fee_rate=str(fee_rate_sat_vb))
        self.generate(self.nodes[2], 1, sync_fun=lambda: self.sync_all(self.nodes[0:3]))

        balance = self.nodes[2].getbalance()
        node_2_bal = self.check_fee_amount(balance, node_2_bal - Decimal('10'), explicit_fee_rate_btc_kvb, self.get_vsize(self.nodes[2].gettransaction(txid)['hex']))
        assert_equal(balance, node_2_bal)
        node_0_bal += Decimal('10')
        assert_equal(self.nodes[0].getbalance(), node_0_bal)

        # Test passing fee_rate as an integer
        amount = Decimal("0.1")
        txid = self.nodes[2].sendmany(amounts={address: amount}, fee_rate=fee_rate_sat_vb)
        self.generate(self.nodes[2], 1, sync_fun=lambda: self.sync_all(self.nodes[0:3]))

        balance = self.nodes[2].getbalance()
        node_2_bal = self.check_fee_amount(balance, node_2_bal - amount, explicit_fee_rate_btc_kvb, self.get_vsize(self.nodes[2].gettransaction(txid)['hex']))
        assert_equal(balance, node_2_bal)
        node_0_bal += amount
        assert_equal(self.nodes[0].getbalance(), node_0_bal)

        for key in ["totalFee", "feeRate"]:
            assert_raises_rpc_error(-8, "Unknown named parameter key", self.nodes[2].sendtoaddress, address=address, amount=1, fee_rate=1, key=1)

        # Test setting explicit fee rate just below the minimum.
        self.log.info("Test sendmany raises 'fee rate too low' if fee_rate of 100 sat/vB is passed")
        assert_raises_rpc_error(-6, "Fee rate (100.000 sat/vB) is lower than the minimum fee rate setting (10000.000 sat/vB)",
            self.nodes[2].sendmany, amounts={address: 10}, fee_rate=100)

        self.log.info("Test sendmany raises if fee_rate of 0 or -1 is passed")
        assert_raises_rpc_error(-6, "Fee rate (0.000 sat/vB) is lower than the minimum fee rate setting (10000.000 sat/vB)",
            self.nodes[2].sendmany, amounts={address: 10}, fee_rate=0)

        assert_raises_rpc_error(-3, OUT_OF_RANGE, self.nodes[2].sendmany, amounts={address: 10}, fee_rate=-1)

        self.log.info("Test sendmany raises if an invalid conf_target or estimate_mode is passed")
        for target, mode in product([-1, 0, 1009], ["economical", "conservative"]):
            assert_raises_rpc_error(-8, "Invalid conf_target, must be between 1 and 1008",  # max value of 1008 per src/policy/fees.h
                self.nodes[2].sendmany, amounts={address: 1}, conf_target=target, estimate_mode=mode)
        for target, mode in product([-1, 0], ["btc/kb", "sat/b"]):
            assert_raises_rpc_error(-8, 'Invalid estimate_mode parameter, must be one of: "unset", "economical", "conservative"',
                self.nodes[2].sendmany, amounts={address: 1}, conf_target=target, estimate_mode=mode)

        self.start_node(3, self.nodes[3].extra_args)
        self.connect_nodes(0, 3)
        self.sync_all()

        # check if we can list zero value tx as available coins
        # 1. create raw_tx
        # 2. hex-changed one output to 0.0
        # 3. sign and send
        # 4. check if recipient (node0) can list the zero value tx
        usp = self.nodes[1].listunspent(query_options={'minimumAmount': '71999.998'})[0]
        inputs = [{"txid": usp['txid'], "vout": usp['vout']}]
        outputs = {self.nodes[1].getnewaddress(): 71999.998, self.nodes[0].getnewaddress(): 11.11}

        raw_tx = self.nodes[1].createrawtransaction(inputs, outputs).replace("c0833842", "00000000")  # replace 11.11 with 0.0 (int32)
        signed_raw_tx = self.nodes[1].signrawtransactionwithwallet(raw_tx)
        decoded_raw_tx = self.nodes[1].decoderawtransaction(signed_raw_tx['hex'])
        zero_value_txid = decoded_raw_tx['txid']
        self.nodes[1].sendrawtransaction(signed_raw_tx['hex'])

        self.sync_all()
        self.generate(self.nodes[1], 1, sync_fun=lambda: self.no_op) # mine a block
        self.sync_all()

        unspent_txs = self.nodes[0].listunspent()  # zero value tx must be in listunspents output
        found = False
        for uTx in unspent_txs:
            if uTx['txid'] == zero_value_txid:
                found = True
                assert_equal(uTx['amount'], Decimal('0'))
        assert found

        self.log.info("Test -walletbroadcast")
        self.stop_nodes()
        self.start_node(0, ["-walletbroadcast=0"])
        self.start_node(1, ["-walletbroadcast=0"])
        self.start_node(2, ["-walletbroadcast=0"])
        self.connect_nodes(0, 1)
        self.connect_nodes(1, 2)
        self.connect_nodes(0, 2)
        self.sync_all(self.nodes[0:3])

        txid_not_broadcast = self.nodes[0].sendtoaddress(self.nodes[2].getnewaddress(), 2)
        tx_obj_not_broadcast = self.nodes[0].gettransaction(txid_not_broadcast)
        self.generate(self.nodes[1], 1, sync_fun=lambda: self.sync_all(self.nodes[0:3]))

        self.get_matured_utxos(2, utxo_seen[2], True, Decimal('71000')) # mark all utxos as seen
        assert_equal(self.nodes[2].getbalance(), node_2_bal)  # should not be changed because tx was not broadcasted

        # now broadcast from another node, mine a block, sync, and check the balance
        self.nodes[1].sendrawtransaction(tx_obj_not_broadcast['hex'])
        self.generate(self.nodes[1], 1, sync_fun=lambda: self.sync_all(self.nodes[0:3]))

        # Get newly matured utxos
        maturedUtxos = self.get_matured_utxos(2, utxo_seen[2], True, Decimal('71000'))
        node_2_bal += sum(utxo['amount'] for utxo in maturedUtxos)

        # Add TX value from 'txid_not_broadcast'
        node_2_bal += 2 # TX value

        tx_obj_not_broadcast = self.nodes[0].gettransaction(tx_obj_not_broadcast['txid'])
        assert_equal(self.nodes[2].getbalance(), node_2_bal)

        # create another tx
        self.nodes[0].sendtoaddress(self.nodes[2].getnewaddress(), 2)

        # restart the nodes with -walletbroadcast=1
        self.stop_nodes()
        self.start_node(0)
        self.start_node(1)
        self.start_node(2)
        self.connect_nodes(0, 1)
        self.connect_nodes(1, 2)
        self.connect_nodes(0, 2)
        self.sync_blocks(self.nodes[0:3])

        self.generate(self.nodes[0], 1, sync_fun=lambda: self.sync_all(self.nodes[0:3]))

        # Get newly matured utxos
        maturedUtxos = self.get_matured_utxos(2, utxo_seen[2], True, Decimal('71000'))
        node_2_bal += sum(utxo['amount'] for utxo in maturedUtxos)

        # Add TX value from 'txid_not_broadcast'
        node_2_bal += 2 # TX value

        # tx should be added to balance because after restarting the nodes tx should be broadcast
        assert_equal(self.nodes[2].getbalance(), node_2_bal)

        # send a tx with value in a string (PR#6380 +)
        txid = self.nodes[0].sendtoaddress(self.nodes[2].getnewaddress(), "2")
        tx_obj = self.nodes[0].gettransaction(txid)
        assert_equal(tx_obj['amount'], Decimal('-2'))

        txid = self.nodes[0].sendtoaddress(self.nodes[2].getnewaddress(), "0.0001")
        tx_obj = self.nodes[0].gettransaction(txid)
        assert_equal(tx_obj['amount'], Decimal('-0.0001'))

        # check if JSON parser can handle scientific notation in strings
        txid = self.nodes[0].sendtoaddress(self.nodes[2].getnewaddress(), "1e-4")
        tx_obj = self.nodes[0].gettransaction(txid)
        assert_equal(tx_obj['amount'], Decimal('-0.0001'))

        # General checks for errors from incorrect inputs
        # This will raise an exception because the amount is negative
        assert_raises_rpc_error(-3, OUT_OF_RANGE, self.nodes[0].sendtoaddress, self.nodes[2].getnewaddress(), "-1")

        # This will raise an exception because the amount type is wrong
        assert_raises_rpc_error(-3, "Invalid amount", self.nodes[0].sendtoaddress, self.nodes[2].getnewaddress(), "1f-4")

        # This will raise an exception since generate does not accept a string
        assert_raises_rpc_error(-1, "not an integer", self.generate, self.nodes[0], "2")

        if not self.options.descriptors:

            # This will raise an exception for the invalid private key format
            assert_raises_rpc_error(-5, "Invalid private key encoding", self.nodes[0].importprivkey, "invalid")

            # This will raise an exception for importing an address with the PS2H flag
            temp_address = self.nodes[1].getnewaddress("", "p2sh-segwit")
            assert_raises_rpc_error(-5, "Cannot use the p2sh flag with an address - use a script instead", self.nodes[0].importaddress, temp_address, "label", False, True)

            # This will raise an exception for attempting to dump the private key of an address you do not own
            assert_raises_rpc_error(-3, "Address does not refer to a key", self.nodes[0].dumpprivkey, temp_address)

            # This will raise an exception for attempting to get the private key of an invalid DigiByte address
            assert_raises_rpc_error(-5, "Invalid DigiByte address", self.nodes[0].dumpprivkey, "invalid")

            # This will raise an exception for attempting to set a label for an invalid DigiByte address
            assert_raises_rpc_error(-5, "Invalid DigiByte address", self.nodes[0].setlabel, "invalid address", "label")

            # This will raise an exception for importing an invalid address
            assert_raises_rpc_error(-5, "Invalid DigiByte address or script", self.nodes[0].importaddress, "invalid")

            # This will raise an exception for attempting to import a pubkey that isn't in hex
            assert_raises_rpc_error(-5, "Pubkey must be a hex string", self.nodes[0].importpubkey, "not hex")

            # This will raise an exception for importing an invalid pubkey
            assert_raises_rpc_error(-5, "Pubkey is not a valid public key", self.nodes[0].importpubkey, "5361746f736869204e616b616d6f746f")

            # Import address and private key to check correct behavior of spendable unspents
            # 1. Send some coins to generate new UTXO
            address_to_import = self.nodes[2].getnewaddress()
            txid = self.nodes[0].sendtoaddress(address_to_import, 1)
            self.generate(self.nodes[0], 1, sync_fun=lambda: self.sync_all(self.nodes[0:3]))


            self.log.info("Test sendtoaddress with fee_rate param (explicit fee rate in sat/vB)")
            self.get_matured_utxos(2, utxo_seen[2]) # clear
            prebalance = self.nodes[2].getbalance()
            assert prebalance > 2
            address = self.nodes[1].getnewaddress()
            amount = 3
            fee_rate_sat_vb = 10000
            fee_rate_btc_kvb = fee_rate_sat_vb * 1e3 / 1e8
            # Test passing fee_rate as an integer
            txid = self.nodes[2].sendtoaddress(address=address, amount=amount, fee_rate=fee_rate_sat_vb)
            tx_size = self.get_vsize(self.nodes[2].gettransaction(txid)['hex'])
            self.generate(self.nodes[0], 1, sync_fun=lambda: self.sync_all(self.nodes[0:3]))

            postbalance = self.nodes[2].getbalance()

            # DGB: Subtract matured UTXO
            maturedUtxos = self.get_matured_utxos(2, utxo_seen[2])
            postbalance -= sum(utxo['amount'] for utxo in maturedUtxos)

            fee = prebalance - postbalance - Decimal(amount)
            assert_fee_amount(fee, tx_size, Decimal(fee_rate_btc_kvb))

            prebalance = self.nodes[2].getbalance()
            amount = Decimal("0.1")
            fee_rate_sat_vb = 10000
            fee_rate_btc_kvb = fee_rate_sat_vb * 1e3 / 1e8

            # Test passing fee_rate as a string
            txid = self.nodes[2].sendtoaddress(address=address, amount=amount, fee_rate=str(fee_rate_sat_vb))
            tx_size = self.get_vsize(self.nodes[2].gettransaction(txid)['hex'])
            self.generate(self.nodes[0], 1, sync_fun=lambda: self.sync_all(self.nodes[0:3]))

            postbalance = self.nodes[2].getbalance()

            # DGB: Subtract matured UTXO
            maturedUtxos = self.get_matured_utxos(2, utxo_seen[2])
            maturedValue = sum(utxo['amount'] for utxo in maturedUtxos)

            fee = prebalance - postbalance - amount + maturedValue
            tx_obj = self.nodes[2].gettransaction(txid)

            assert_fee_amount(fee, tx_size, Decimal(fee_rate_btc_kvb))

            for key in ["totalFee", "feeRate"]:
                assert_raises_rpc_error(-8, "Unknown named parameter key", self.nodes[2].sendtoaddress, address=address, amount=1, fee_rate=1, key=1)

            # Test setting explicit fee rate just below the minimum.
            self.log.info("Test sendtoaddress raises 'fee rate too low' if fee_rate of 99.999 is passed")
            assert_raises_rpc_error(-6, "Fee rate (99.999 sat/vB) is lower than the minimum fee rate setting (10000.000 sat/vB)", 
                self.nodes[2].sendtoaddress, address=address, amount=1, fee_rate=99.999)
            self.log.info("Test sendtoaddress raises if fee_rate of 0 or -1 is passed")
            assert_raises_rpc_error(-6, "Fee rate (0.000 sat/vB) is lower than the minimum fee rate setting (10000.000 sat/vB)",
                self.nodes[2].sendtoaddress, address=address, amount=10, fee_rate=0)
            assert_raises_rpc_error(-3, OUT_OF_RANGE, self.nodes[2].sendtoaddress, address=address, amount=1.0, fee_rate=-1)

            self.log.info("Test sendtoaddress raises if an invalid conf_target or estimate_mode is passed")
            for target, mode in product([-1, 0, 1009], ["economical", "conservative"]):
                assert_raises_rpc_error(-8, "Invalid conf_target, must be between 1 and 1008",  # max value of 1008 per src/policy/fees.h
                    self.nodes[2].sendtoaddress, address=address, amount=1, conf_target=target, estimate_mode=mode)
            for target, mode in product([-1, 0], ["btc/kb", "sat/b"]):
                assert_raises_rpc_error(-8, 'Invalid estimate_mode parameter, must be one of: "unset", "economical", "conservative"',
                    self.nodes[2].sendtoaddress, address=address, amount=1, conf_target=target, estimate_mode=mode)

            # 2. Import address from node2 to node1
            self.nodes[1].importaddress(address_to_import)

            # 3. Validate that the imported address is watch-only on node1
            assert self.nodes[1].getaddressinfo(address_to_import)["iswatchonly"]

            # 4. Check that the unspents after import are not spendable
            assert_array_result(self.nodes[1].listunspent(),
                                {"address": address_to_import},
                                {"spendable": False})

            # 5. Import private key of the previously imported address on node1
            priv_key = self.nodes[2].dumpprivkey(address_to_import)
            self.nodes[1].importprivkey(priv_key)

            # 6. Check that the unspents are now spendable on node1
            assert_array_result(self.nodes[1].listunspent(),
                                {"address": address_to_import},
                                {"spendable": True})

        # Mine a block from node0 to an address from node1
        coinbase_addr = self.nodes[1].getnewaddress()
        block_hash = self.generatetoaddress(self.nodes[0], 1, coinbase_addr, sync_fun=lambda: self.sync_all(self.nodes[0:3]))[0]
        coinbase_txid = self.nodes[0].getblock(block_hash)['tx'][0]
        self.sync_all(self.nodes[0:3])

        # Check that the txid and balance is found by node1
        self.nodes[1].gettransaction(coinbase_txid)

        # check if wallet or blockchain maintenance changes the balance
        self.sync_all(self.nodes[0:3])
        
        blocks =  self.generate(self.nodes[0], 2, sync_fun=lambda: self.sync_all(self.nodes[0:3]))
        balance_nodes = [self.nodes[i].getbalance() for i in range(3)]
        block_count = self.nodes[0].getblockcount()

        # Check modes:
        #   - True: unicode escaped as \u....
        #   - False: unicode directly as UTF-8
        for mode in [True, False]:
            self.nodes[0].rpc.ensure_ascii = mode
            # unicode check: Basic Multilingual Plane, Supplementary Plane respectively
            for label in [u'рыба', u'𝅘𝅥𝅯']:
                addr = self.nodes[0].getnewaddress()
                self.nodes[0].setlabel(addr, label)
                test_address(self.nodes[0], addr, labels=[label])
                assert label in self.nodes[0].listlabels()
        self.nodes[0].rpc.ensure_ascii = True  # restore to default

        # maintenance tests
        maintenance = [
            '-rescan',
            '-reindex',
        ]
        chainlimit = 6

        # if --legacy-wallet is used, it will call generate() a few times,
        # which will cause some mined coins to mature. Hence, we increase
        # the chainlimit multiplicator for long transactions, so that
        # the wallet will reject the long transaction issued by this test.
        M = 3 if not self.options.descriptors else 2

        for m in maintenance:
            self.log.info("Test " + m)
            self.stop_nodes()
            # set lower ancestor limit for later
            self.start_node(0, [m, "-limitancestorcount=" + str(chainlimit)])
            self.start_node(1, [m, "-limitancestorcount=" + str(chainlimit)])
            self.start_node(2, [m, "-limitancestorcount=" + str(chainlimit)])
            if m == '-reindex':
                # reindex will leave rpc warm up "early"; Wait for it to finish
                self.wait_until(lambda: [block_count] * 3 == [self.nodes[i].getblockcount() for i in range(3)])
            assert_equal(balance_nodes, [self.nodes[i].getbalance() for i in range(3)])

        # Exercise listsinceblock with the last two blocks
        coinbase_tx_1 = self.nodes[0].listsinceblock(blocks[0])
        assert_equal(coinbase_tx_1["lastblock"], blocks[1])
        assert_equal(len(coinbase_tx_1["transactions"]), 1)
        assert_equal(coinbase_tx_1["transactions"][0]["blockhash"], blocks[1])
        assert_equal(len(self.nodes[0].listsinceblock(blocks[1])["transactions"]), 0)

        # ==Check that wallet prefers to use coins that don't exceed mempool limits =====

        # Get all non-zero utxos together
        chain_addrs = [self.nodes[0].getnewaddress(), self.nodes[0].getnewaddress()]
        singletxid = self.nodes[0].sendtoaddress(chain_addrs[0], self.nodes[0].getbalance(), "", "", True)
        self.generate(self.nodes[0], 1, sync_fun=self.no_op)
        node0_balance = self.nodes[0].getbalance()
        # Split into two chains
        rawtx = self.nodes[0].createrawtransaction([{"txid": singletxid, "vout": 0}], {chain_addrs[0]: node0_balance / 2 - Decimal('0.1'), chain_addrs[1]: node0_balance / 2 - Decimal('0.1')})
        signedtx = self.nodes[0].signrawtransactionwithwallet(rawtx)
        singletxid = self.nodes[0].sendrawtransaction(hexstring=signedtx["hex"], maxfeerate=0)
        self.generate(self.nodes[0], 1, sync_fun=self.no_op)

        # Make a long chain of unconfirmed payments without hitting mempool limit
        # Each tx we make leaves only one output of change on a chain 1 longer
        # Since the amount to send is always much less than the outputs, we only ever need one output
        # So we should be able to generate exactly chainlimit txs for each original output
        sending_addr = self.nodes[1].getnewaddress()
        txid_list = []
        #Generate 93 more blocks to have sufficient funds to cover fees, (100 block maturation)
        self.generate(self.nodes[0], 93, sync_fun=self.no_op)
        for _ in range(chainlimit * M):
            tx_id = self.nodes[0].sendtoaddress(sending_addr, Decimal('0.1'))
            txid_list.append(tx_id)

        assert_equal(self.nodes[0].getmempoolinfo()['size'], chainlimit * M)
        assert_equal(len(txid_list), chainlimit * M)

        # Without walletrejectlongchains, we will still generate a txid
        # The tx will be stored in the wallet but not accepted to the mempool
        
        if self.options.descriptors:
            extra_txid = self.nodes[0].sendtoaddress(sending_addr, Decimal('0.1'))
            assert extra_txid not in self.nodes[0].getrawmempool()
            assert extra_txid in [tx["txid"] for tx in self.nodes[0].listtransactions()]
            self.nodes[0].abandontransaction(extra_txid)
            total_txs = len(self.nodes[0].listtransactions("*", 99999))
        # legacy-wallet bypass test. This edit bypasses the above abandon transaction test for legacy-wallet. Descriptors wallet working as expected.
        # For whatever reason it already gets added to mempool and no tx to abandon for rawmempool. This needs to be sorted with dandelion and mempool behavior.
        else:
            total_txs = len(self.nodes[0].listtransactions("*", 99999))

        # Try with walletrejectlongchains
        # Double chain limit but require combining inputs, so we pass SelectCoinsMinConf
        self.stop_node(0)
        extra_args = ["-walletrejectlongchains", "-limitancestorcount=" + str(2 * chainlimit)]
        self.start_node(0, extra_args=extra_args)

        # wait until the wallet has submitted all transactions to the mempool
        self.wait_until(lambda: len(self.nodes[0].getrawmempool()) == chainlimit * M)

        # Prevent potential race condition when calling wallet RPCs right after restart
        self.nodes[0].syncwithvalidationinterfacequeue()

        node0_balance = self.nodes[0].getbalance()
        # With walletrejectlongchains we will not create the tx and store it in our wallet.
        assert_raises_rpc_error(-6, "Transaction has too long of a mempool chain", self.nodes[0].sendtoaddress, sending_addr, node0_balance - Decimal('0.1'))

        # Verify nothing new in wallet
        assert_equal(total_txs, len(self.nodes[0].listtransactions("*", 99999)))

        # Test getaddressinfo on external address. Note that these addresses are taken from disablewallet.py
        assert_raises_rpc_error(-5, "Invalid address", self.nodes[0].getaddressinfo, "shgBKbJT4FZ42JwVrTi289Mef2SzAK1ajy")
        address_info = self.nodes[0].getaddressinfo("ssbV6ZkjEpA4tvLBBtnXPm7Xm2nojKoyR7")
        assert_equal(address_info['address'], "ssbV6ZkjEpA4tvLBBtnXPm7Xm2nojKoyR7")
        assert_equal(address_info["scriptPubKey"], "76a9146dc8a87cffeeac588f603dd95833cb66693c360888ac")
        assert not address_info["ismine"]
        assert not address_info["iswatchonly"]
        assert not address_info["isscript"]
        assert not address_info["ischange"]

        # Test getaddressinfo 'ischange' field on change address.
        self.generate(self.nodes[0], 1, sync_fun=self.no_op)
        destination = self.nodes[1].getnewaddress()
        txid = self.nodes[0].sendtoaddress(destination, 0.123)
        tx = self.nodes[0].decoderawtransaction(self.nodes[0].gettransaction(txid)['hex'])

        output_addresses = [vout['scriptPubKey']['address'] for vout in tx["vout"]]
        assert len(output_addresses) > 1
        for address in output_addresses:
            ischange = self.nodes[0].getaddressinfo(address)['ischange']
            assert_equal(ischange, address != destination)
            if ischange:
                change = address
        self.nodes[0].setlabel(change, 'foobar')
        assert_equal(self.nodes[0].getaddressinfo(change)['ischange'], False)

        # Test gettransaction response with different arguments.
        self.log.info("Testing gettransaction response with different arguments...")
        self.nodes[0].setlabel(change, 'baz')
        baz = self.nodes[0].listtransactions(label="baz", count=1)[0]
        expected_receive_vout = {"label":    "baz",
                                 "address":  baz["address"],
                                 "amount":   baz["amount"],
                                 "category": baz["category"],
                                 "vout":     baz["vout"]}
        expected_fields = frozenset({'amount', 'bip125-replaceable', 'confirmations', 'details', 'fee',
                                     'hex', 'time', 'timereceived', 'trusted', 'txid', 'walletconflicts'})
        verbose_field = "decoded"
        expected_verbose_fields = expected_fields | {verbose_field}

        self.log.debug("Testing gettransaction response without verbose")
        tx = self.nodes[0].gettransaction(txid=txid)
        assert_equal(set([*tx]), expected_fields)
        assert_array_result(tx["details"], {"category": "receive"}, expected_receive_vout)

        self.log.debug("Testing gettransaction response with verbose set to False")
        tx = self.nodes[0].gettransaction(txid=txid, verbose=False)
        assert_equal(set([*tx]), expected_fields)
        assert_array_result(tx["details"], {"category": "receive"}, expected_receive_vout)

        self.log.debug("Testing gettransaction response with verbose set to True")
        tx = self.nodes[0].gettransaction(txid=txid, verbose=True)
        assert_equal(set([*tx]), expected_verbose_fields)
        assert_array_result(tx["details"], {"category": "receive"}, expected_receive_vout)
        assert_equal(tx[verbose_field], self.nodes[0].decoderawtransaction(tx["hex"]))

        self.log.info("Test send* RPCs with verbose=True")
        address = self.nodes[0].getnewaddress("test")
        txid_feeReason_one = self.nodes[2].sendtoaddress(address=address, amount=5, verbose=True)
        assert_equal(txid_feeReason_one["fee_reason"], "Minimum Required Fee")
        txid_feeReason_two = self.nodes[2].sendmany(dummy='', amounts={address: 5}, verbose=True)
        assert_equal(txid_feeReason_two["fee_reason"], "Minimum Required Fee")
        self.log.info("Test send* RPCs with verbose=False")
        txid_feeReason_three = self.nodes[2].sendtoaddress(address=address, amount=5, verbose=False)
        assert_equal(self.nodes[2].gettransaction(txid_feeReason_three)['txid'], txid_feeReason_three)
        txid_feeReason_four = self.nodes[2].sendmany(dummy='', amounts={address: 5}, verbose=False)
        assert_equal(self.nodes[2].gettransaction(txid_feeReason_four)['txid'], txid_feeReason_four)


if __name__ == '__main__':
    WalletTest().main()
