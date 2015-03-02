# Copyright (c) 2015 Mirantis, Inc.
# All Rights Reserved.
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

import eventlet
import mock

from oslo_config import cfg
from oslo_utils import importutils

from neutron.cmd.sanity import checks
from neutron.plugins.openvswitch.agent import ovs_neutron_agent as ovsagt
from neutron.plugins.openvswitch.common import constants
from neutron.tests.common import net_helpers
from neutron.tests.functional.agent.linux import base
from neutron.tests.functional.agent.linux import helpers
from neutron.tests.functional.agent import test_ovs_lib


cfg.CONF.import_group('OVS', 'neutron.plugins.openvswitch.common.config')


class _OVSAgentTestBase(base.BaseIPVethTestCase,
                        test_ovs_lib.OVSBridgeTestBase):
    def setUp(self):
        super(_OVSAgentTestBase, self).setUp()
        self.br = self.useFixture(net_helpers.OVSBridgeFixture()).bridge
        self.of_interface_mod = importutils.import_module(self._MAIN_MODULE)
        self.br_int_cls = None
        self.br_tun_cls = None
        self.br_phys_cls = None
        self.br_int = None
        self.init_done = False
        self.init_done_ev = eventlet.event.Event()
        self._main_thread = eventlet.spawn(self._kick_main)
        self.addCleanup(self._kill_main)

        # wait for _kick_main -> of_interface main -> _agent_main
        while not self.init_done:
            self.init_done_ev.wait()

    def _kick_main(self):
        with mock.patch.object(ovsagt, 'main', self._agent_main):
            self.of_interface_mod.main()

    def _kill_main(self):
        self._main_thread.kill()
        self._main_thread.wait()

    def _agent_main(self, bridge_classes):
        self.br_int_cls = bridge_classes['br_int']
        self.br_phys_cls = bridge_classes['br_phys']
        self.br_tun_cls = bridge_classes['br_tun']
        self.br_int = self.br_int_cls(self.br.br_name)
        self.br_int.set_secure_mode()
        self.br_int.setup_controllers(cfg.CONF)
        self.br_int.setup_default_table()

        # signal to setUp()
        self.init_done = True
        self.init_done_ev.send()


class _OVSAgentOFCtlTestBase(_OVSAgentTestBase):
    _MAIN_MODULE = 'neutron.plugins.openvswitch.agent.openflow.ovs_ofctl.main'


class _ARPSpoofTestCase(object):

    def setUp(self):
        if not checks.arp_header_match_supported():
            self.skipTest("ARP header matching not supported")
        # NOTE(kevinbenton): it would be way cooler to use scapy for
        # these but scapy requires the python process to be running as
        # root to bind to the ports.
        super(_ARPSpoofTestCase, self).setUp()
        self.src_addr = '192.168.0.1'
        self.dst_addr = '192.168.0.2'
        self.src_ns = self._create_namespace()
        self.dst_ns = self._create_namespace()
        self.pinger = helpers.Pinger(self.src_ns, max_attempts=2)
        self.src_p = self.useFixture(
            net_helpers.OVSPortFixture(self.br, self.src_ns.namespace)).port
        self.dst_p = self.useFixture(
            net_helpers.OVSPortFixture(self.br, self.dst_ns.namespace)).port
        # wait to add IPs until after anti-spoof rules to ensure ARP doesn't
        # happen before

    def test_arp_spoof_doesnt_block_normal_traffic(self):
        self._setup_arp_spoof_for_port(self.src_p.name, [self.src_addr])
        self._setup_arp_spoof_for_port(self.dst_p.name, [self.dst_addr])
        self.src_p.addr.add('%s/24' % self.src_addr)
        self.dst_p.addr.add('%s/24' % self.dst_addr)
        self.pinger.assert_ping(self.dst_addr)

    def test_arp_spoof_blocks_response(self):
        # this will prevent the destination from responding to the ARP
        # request for it's own address
        self._setup_arp_spoof_for_port(self.dst_p.name, ['192.168.0.3'])
        self.src_p.addr.add('%s/24' % self.src_addr)
        self.dst_p.addr.add('%s/24' % self.dst_addr)
        self.pinger.assert_no_ping(self.dst_addr)

    def test_arp_spoof_allowed_address_pairs(self):
        self._setup_arp_spoof_for_port(self.dst_p.name, ['192.168.0.3',
                                                         self.dst_addr])
        self.src_p.addr.add('%s/24' % self.src_addr)
        self.dst_p.addr.add('%s/24' % self.dst_addr)
        self.pinger.assert_ping(self.dst_addr)

    def test_arp_spoof_disable_port_security(self):
        # block first and then disable port security to make sure old rules
        # are cleared
        self._setup_arp_spoof_for_port(self.dst_p.name, ['192.168.0.3'])
        self._setup_arp_spoof_for_port(self.dst_p.name, ['192.168.0.3'],
                                       psec=False)
        self.src_p.addr.add('%s/24' % self.src_addr)
        self.dst_p.addr.add('%s/24' % self.dst_addr)
        self.pinger.assert_ping(self.dst_addr)

    def _setup_arp_spoof_for_port(self, port, addrs, psec=True):
        of_port_map = self.br.get_vif_port_to_ofport_map()

        class VifPort(object):
            ofport = of_port_map[port]
            port_name = port

        ip_addr = addrs.pop()
        details = {'port_security_enabled': psec,
                   'fixed_ips': [{'ip_address': ip_addr}],
                   'allowed_address_pairs': [
                        dict(ip_address=ip) for ip in addrs]}
        ovsagt.OVSNeutronAgent.setup_arp_spoofing_protection(
            self.br_int, VifPort(), details)


class ARPSpoofOFCtlTestCase(_ARPSpoofTestCase, _OVSAgentOFCtlTestBase):
    pass


class _CanaryTableTestCase(object):
    def test_canary_table(self):
        self.br_int.delete_flows()
        self.assertEqual(constants.OVS_RESTARTED,
                         self.br_int.check_canary_table())
        self.br_int.setup_canary_table()
        self.assertEqual(constants.OVS_NORMAL,
                         self.br_int.check_canary_table())


class CanaryTableOFCtlTestCase(_CanaryTableTestCase, _OVSAgentOFCtlTestBase):
    pass
