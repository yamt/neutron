# Copyright (C) 2014 VA Linux Systems Japan K.K.
# Copyright (C) 2014 YAMAMOTO Takashi <yamamoto at valinux co jp>
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

from neutron.agent.linux import ovs_lib
from neutron.tests.functional.agent.linux import base


class TestOvsLib(base.BaseOVSLinuxTestCase):
    def setUp(self):
        super(TestOvsLib, self).setUp()
        self.bridge = self.create_ovs_bridge()

    def test_get_bridge_name_for_datapath_id(self):
        datapath_id = self.bridge.get_datapath_id()
        name = ovs_lib.get_bridge_name_for_datapath_id(self.root_helper,
                                                       datapath_id)
        self.assertEqual(self.bridge.br_name, name)
