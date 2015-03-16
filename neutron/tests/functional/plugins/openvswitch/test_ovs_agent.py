# Copyright (C) 2015 VA Linux Systems Japan K.K.
# Copyright (C) 2015 YAMAMOTO Takashi <yamamoto at valinux co jp>
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

from neutron.plugins.openvswitch.agent import ovs_neutron_agent
from neutron.plugins.openvswitch.common import constants
from neutron.tests.common import net_helpers
from neutron.tests.functional.agent.linux import base


cfg.CONF.import_group('OVS', 'neutron.plugins.openvswitch.common.config')


class _OVSAgentTestCase(object):
    def setUp(self):
        super(_OVSAgentTestCase, self).setUp()
        self.br = self.useFixture(net_helpers.OVSBridgeFixture()).bridge
        self.driver_main_mod = importutils.import_module(self._MAIN_MODULE)
        self.driver_args = None
        self.br_int_cls = None
        self.br_tun_cls = None
        self.br_phys_cls = None
        self.br_int = None
        self.init_done = False
        self.init_done_ev = eventlet.event.Event()
        self._main_thread = eventlet.spawn(self._kick_main)
        self.addCleanup(self._kill_main)
        while not self.init_done:
            self.init_done_ev.wait()

    def _kick_main(self):
        with mock.patch.object(ovs_neutron_agent, 'main', self._agent_main):
            self.driver_main_mod.main()

    def _kill_main(self):
        self._main_thread.kill()
        self._main_thread.wait()

    def _agent_main(self, backend_info):
        (self.driver_args,
         self.br_int_cls,
         self.br_phys_cls,
         self.br_tun_cls) = backend_info
        self.br_int = self.br_int_cls(self.br.br_name,
                                      driver_args=self.driver_args)
        self.br_int.set_secure_mode()
        self.br_int.setup_controllers(cfg.CONF)
        self.init_done = True
        self.init_done_ev.send()

    def test_canary_table(self):
        self.assertEqual(constants.OVS_RESTARTED,
                         self.br_int.check_canary_table())
        self.br_int.setup_canary_table()
        self.assertEqual(constants.OVS_NORMAL,
                         self.br_int.check_canary_table())


class OVSAgentOFCtlTestCase(_OVSAgentTestCase, base.BaseOVSLinuxTestCase):
    _MAIN_MODULE = 'neutron.plugins.openvswitch.agent.ovs_ofctl.main'


class OVSAgentRyuTestCase(_OVSAgentTestCase, base.BaseOVSLinuxTestCase):
    # NOTE(yamamoto): This case tries to listen on tcp:127.0.0.1:6633.
    # It would fail if there's other process listening on the port already.
    # REVISIT(yamamoto): This case might leave some threads.  Probably it's
    # better to run this in a separate process.
    _MAIN_MODULE = 'neutron.plugins.openvswitch.agent.ryu.main'

    def setUp(self):
        try:
            import ryu
        except ImportError:
            self.skipTest('ryu is not importable')
        if not ryu.version_info >= (3, 19):
            self.skipTest('ryu>=3.19 is necessary')
        super(OVSAgentRyuTestCase, self).setUp()
