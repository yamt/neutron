# Copyright (C) 2014 VA Linux Systems Japan K.K.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
# @author: Fumihiko Kakuma, VA Linux Systems Japan K.K.

import collections
import contextlib

import mock

from neutron.openstack.common import importutils
from neutron.tests.unit.ofagent import ofa_test_base


_OFALIB_NAME = 'neutron.plugins.ofagent.agent.arp_lib'


class OFAAgentTestCase(ofa_test_base.OFAAgentTestBase):

    def setUp(self):
        super(OFAAgentTestCase, self).setUp()

        Net = collections.namedtuple('Net', 'net, mac, ip')
        self.nets = [Net(net=10, mac='11:11:11:44:55:66', ip='10.1.2.20'),
                     Net(net=10, mac='11:11:11:44:55:67', ip='10.1.2.21'),
                     Net(net=20, mac='22:22:22:44:55:66', ip='10.2.2.20')]

        self.packet_mod = mock.Mock()
        self.proto_ethernet_mod = mock.Mock()
        self.proto_vlan_mod = mock.Mock()
        self.proto_vlan_mod.vid = self.nets[0].net
        self.proto_arp_mod = mock.Mock()
        self.fake_get_protocol = mock.Mock(return_value=self.proto_vlan_mod)
        self.packet_mod.get_protocol = self.fake_get_protocol
        self.fake_add_protocol = mock.Mock()
        self.packet_mod.add_protocol = self.fake_add_protocol
        self.arp = importutils.import_module('ryu.lib.packet.arp')
        self.arp_arp = 'arp_arp'
        self.arp.arp = mock.Mock(return_value=self.arp_arp)
        self.ethernet = importutils.import_module('ryu.lib.packet.ethernet')
        self.ethernet_ethernet = 'ethernet_ethernet'
        self.ethernet.ethernet = mock.Mock(return_value=self.ethernet_ethernet)
        self.vlan = importutils.import_module('ryu.lib.packet.vlan')
        self.vlan_vlan = mock.Mock()
        self.vlan.vlan = mock.Mock(return_value=self.vlan_vlan)
        self.Packet = importutils.import_module('ryu.lib.packet.packet.Packet')
        self.Packet.return_value = self.packet_mod

        self.ryuapp = 'ryuapp'
        self.inport = '1'
        self.ev = mock.Mock()
        self.datapath = self._mk_test_dp('tun_br')
        self.ofproto = importutils.import_module('ryu.ofproto.ofproto_v1_3')
        self.ofpp = mock.Mock()
        self.datapath.ofproto = self.ofproto
        self.datapath.ofproto_parser = self.ofpp
        self.OFPActionOutput = mock.Mock()
        self.OFPActionOutput.return_value = 'OFPActionOutput'
        self.ofpp.OFPActionOutput = self.OFPActionOutput
        self.msg = mock.Mock()
        self.msg.datapath = self.datapath
        self.msg.buffer_id = self.ofproto.OFP_NO_BUFFER
        self.msg_data = 'test_message_data'
        self.msg.data = self.msg_data
        self.ev.msg = self.msg
        self.msg.match = {'in_port': self.inport}


class TestArpLib(OFAAgentTestCase):

    def setUp(self):
        super(TestArpLib, self).setUp()

        self.mod_arplib = importutils.import_module(_OFALIB_NAME)
        self.arplib = self.mod_arplib.ArpLib(self.ryuapp)
        self.packet_mod.get_protocol = self._fake_get_protocol
        self._fake_get_protocol_ethernet = True
        self._fake_get_protocol_vlan = True
        self._fake_get_protocol_arp = True

    def test__send_unknown_packet_no_buffer(self):
        in_port = 3
        out_port = self.ofproto.OFPP_TABLE
        self.msg.buffer_id = self.ofproto.OFP_NO_BUFFER
        self.arplib._send_unknown_packet(self.msg, in_port, out_port)
        actions = [self.ofpp.OFPActionOutput(self.ofproto.OFPP_TABLE, 0)]
        self.ofpp.OFPPacketOut.assert_called_once_with(
            datapath=self.datapath,
            buffer_id=self.msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=self.msg_data)

    def test__send_unknown_packet_existence_buffer(self):
        in_port = 3
        out_port = self.ofproto.OFPP_TABLE
        self.msg.buffer_id = 256
        self.arplib._send_unknown_packet(self.msg, in_port, out_port)
        actions = [self.ofpp.OFPActionOutput(self.ofproto.OFPP_TABLE, 0)]
        self.ofpp.OFPPacketOut.assert_called_once_with(
            datapath=self.datapath,
            buffer_id=self.msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=None)

    def test__respond_arp(self):
        self.arplib._arp_tbl = {
            self.nets[0].net: {self.nets[0].ip: self.nets[0].mac}}
        port = 3
        arptbl = self.arplib._arp_tbl[self.nets[0].net]
        pkt_ethernet = mock.Mock()
        pkt_vlan = mock.Mock()
        pkt_arp = mock.Mock()
        pkt_arp.opcode = self.arp.ARP_REQUEST
        pkt_arp.dst_ip = self.nets[0].ip
        with mock.patch.object(
            self.arplib, '_send_arp_reply'
        ) as send_arp_rep_fn:
            self.assertTrue(
                self.arplib._respond_arp(self.datapath, port, arptbl,
                                         pkt_ethernet, pkt_vlan, pkt_arp))
        self.assertEqual([mock.call(self.ethernet_ethernet),
                          mock.call(self.vlan_vlan),
                          mock.call(self.arp_arp)],
                         self.fake_add_protocol.call_args_list)
        send_arp_rep_fn.assert_called_once_with(
            self.datapath, port, self.packet_mod)

    def test__respond_arp_non_arp_req(self):
        self.arplib._arp_tbl = {
            self.nets[0].net: {self.nets[0].ip: self.nets[0].mac}}
        port = 3
        arptbl = self.arplib._arp_tbl[self.nets[0].net]
        pkt_ethernet = mock.Mock()
        pkt_vlan = mock.Mock()
        pkt_arp = mock.Mock()
        pkt_arp.opcode = self.arp.ARP_REPLY
        self.assertFalse(
            self.arplib._respond_arp(self.datapath, port, arptbl,
                                     pkt_ethernet, pkt_vlan, pkt_arp))

    def test__respond_arp_ip_not_found_in_arptable(self):
        self.arplib._arp_tbl = {
            self.nets[0].net: {self.nets[0].ip: self.nets[0].mac}}
        port = 3
        arptbl = self.arplib._arp_tbl[self.nets[0].net]
        pkt_ethernet = mock.Mock()
        pkt_vlan = mock.Mock()
        pkt_arp = mock.Mock()
        pkt_arp.opcode = self.arp.ARP_REQUEST
        pkt_arp.dst_ip = self.nets[1].ip
        self.assertFalse(
            self.arplib._respond_arp(self.datapath, port, arptbl,
                                     pkt_ethernet, pkt_vlan, pkt_arp))

    def test_add_arp_table_entry(self):
        self.arplib.add_arp_table_entry(self.nets[0].net,
                                        self.nets[0].ip, self.nets[0].mac)
        self.assertEqual(
            self.arplib._arp_tbl,
            {self.nets[0].net: {self.nets[0].ip: self.nets[0].mac}})

    def test_add_arp_table_entry_multiple_net(self):
        self.arplib.add_arp_table_entry(self.nets[0].net,
                                        self.nets[0].ip, self.nets[0].mac)
        self.arplib.add_arp_table_entry(self.nets[2].net,
                                        self.nets[2].ip, self.nets[2].mac)
        self.assertEqual(
            self.arplib._arp_tbl,
            {self.nets[0].net: {self.nets[0].ip: self.nets[0].mac},
            self.nets[2].net: {self.nets[2].ip: self.nets[2].mac}})

    def test_add_arp_table_entry_multiple_ip(self):
        self.arplib.add_arp_table_entry(self.nets[0].net,
                                        self.nets[0].ip, self.nets[0].mac)
        self.arplib.add_arp_table_entry(self.nets[0].net,
                                        self.nets[1].ip, self.nets[1].mac)
        self.assertEqual(
            self.arplib._arp_tbl,
            {self.nets[0].net: {self.nets[0].ip: self.nets[0].mac,
                                self.nets[1].ip: self.nets[1].mac}})

    def test_del_arp_table_entry(self):
        self.arplib._arp_tbl = {
            self.nets[0].net: {self.nets[0].ip: self.nets[0].mac}}
        self.arplib.del_arp_table_entry(self.nets[0].net, self.nets[0].ip)
        self.assertEqual(self.arplib._arp_tbl, {})

    def test_del_arp_table_entry_multiple_net(self):
        self.arplib._arp_tbl = {
            self.nets[0].net: {self.nets[0].ip: self.nets[0].mac},
            self.nets[2].net: {self.nets[2].ip: self.nets[2].mac}}
        self.arplib.del_arp_table_entry(self.nets[0].net, self.nets[0].ip)
        self.assertEqual(
            self.arplib._arp_tbl,
            {self.nets[2].net: {self.nets[2].ip: self.nets[2].mac}})

    def test_del_arp_table_entry_multiple_ip(self):
        self.arplib._arp_tbl = {
            self.nets[0].net: {self.nets[0].ip: self.nets[0].mac,
                               self.nets[1].ip: self.nets[1].mac}}
        self.arplib.del_arp_table_entry(self.nets[0].net, self.nets[1].ip)
        self.assertEqual(
            self.arplib._arp_tbl,
            {self.nets[0].net: {self.nets[0].ip: self.nets[0].mac}})

    def _fake_get_protocol(self, net_type):
        if net_type == self.ethernet.ethernet:
            if self._fake_get_protocol_ethernet:
                return self.proto_ethernet_mod
            else:
                return
        if net_type == self.vlan.vlan:
            if self._fake_get_protocol_vlan:
                return self.proto_vlan_mod
            else:
                return
        if net_type == self.arp.arp:
            if self._fake_get_protocol_arp:
                return self.proto_arp_mod
            else:
                return

    def test_packet_in_handler(self):
        self.arplib._arp_tbl = {
            self.nets[0].net: {self.nets[0].ip: self.nets[0].mac}}
        with contextlib.nested(
            mock.patch.object(self.arplib, '_respond_arp',
                              return_value=True),
            mock.patch.object(self.arplib,
                              '_add_flow_to_avoid_unknown_packet'),
            mock.patch.object(self.arplib,
                              '_send_unknown_packet'),
        ) as (res_arp_fn, add_flow_fn, send_unknown_pk_fn):
            self.arplib.packet_in_handler(self.ev)
        self.assertFalse(add_flow_fn.call_count)
        self.assertFalse(send_unknown_pk_fn.call_count)
        res_arp_fn.assert_called_once_with(
            self.datapath, self.inport,
            self.arplib._arp_tbl[self.nets[0].net],
            self.proto_ethernet_mod, self.proto_vlan_mod, self.proto_arp_mod)

    def test_packet_in_handler_non_ethernet(self):
        self.arplib._arp_tbl = {
            self.nets[0].net: {self.nets[0].ip: self.nets[0].mac}}
        self._fake_get_protocol_ethernet = False
        with contextlib.nested(
            mock.patch.object(self.arplib, '_respond_arp',
                              return_value=True),
            mock.patch.object(self.arplib,
                              '_add_flow_to_avoid_unknown_packet'),
            mock.patch.object(self.arplib,
                              '_send_unknown_packet'),
        ) as (res_arp_fn, add_flow_fn, send_unknown_pk_fn):
            self.arplib.packet_in_handler(self.ev)
        self.assertFalse(add_flow_fn.call_count)
        self.assertFalse(send_unknown_pk_fn.call_count)
        self.assertFalse(res_arp_fn.call_count)

    def test_packet_in_handler_non_vlan(self):
        self.arplib._arp_tbl = {
            self.nets[0].net: {self.nets[0].ip: self.nets[0].mac}}
        self._fake_get_protocol_vlan = False
        with contextlib.nested(
            mock.patch.object(self.arplib, '_respond_arp',
                              return_value=True),
            mock.patch.object(self.arplib,
                              '_add_flow_to_avoid_unknown_packet'),
            mock.patch.object(self.arplib,
                              '_send_unknown_packet'),
        ) as (res_arp_fn, add_flow_fn, send_unknown_pk_fn):
            self.arplib.packet_in_handler(self.ev)
        self.assertFalse(add_flow_fn.call_count)
        self.assertFalse(send_unknown_pk_fn.call_count)
        self.assertFalse(res_arp_fn.call_count)

    def test_packet_in_handler_non_arp(self):
        self.arplib._arp_tbl = {
            self.nets[0].net: {self.nets[0].ip: self.nets[0].mac}}
        self._fake_get_protocol_arp = False
        with contextlib.nested(
            mock.patch.object(self.arplib, '_respond_arp',
                              return_value=True),
            mock.patch.object(self.arplib,
                              '_add_flow_to_avoid_unknown_packet'),
            mock.patch.object(self.arplib,
                              '_send_unknown_packet'),
        ) as (res_arp_fn, add_flow_fn, send_unknown_pk_fn):
            self.arplib.packet_in_handler(self.ev)
        self.assertFalse(add_flow_fn.call_count)
        self.assertFalse(send_unknown_pk_fn.call_count)
        self.assertFalse(res_arp_fn.call_count)

    def test_packet_in_handler_unknown_network(self):
        self.arplib._arp_tbl = {
            self.nets[0].net: {self.nets[0].ip: self.nets[0].mac}}
        with contextlib.nested(
            mock.patch.object(self.arplib, '_respond_arp',
                              return_value=False),
            mock.patch.object(self.arplib,
                              '_add_flow_to_avoid_unknown_packet'),
            mock.patch.object(self.arplib,
                              '_send_unknown_packet'),
        ) as (res_arp_fn, add_flow_fn, send_unknown_pk_fn):
            self.arplib.packet_in_handler(self.ev)
        self.assertEqual(add_flow_fn.call_count, 1)
        self.assertEqual(send_unknown_pk_fn.call_count, 1)
        res_arp_fn.assert_called_once_with(
            self.datapath, self.inport,
            self.arplib._arp_tbl[self.nets[0].net],
            self.proto_ethernet_mod, self.proto_vlan_mod, self.proto_arp_mod)
