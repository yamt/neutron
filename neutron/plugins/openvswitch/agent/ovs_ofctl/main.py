# Copyright (C) 2014,2015 VA Linux Systems Japan K.K.
# Copyright (C) 2014 Fumihiko Kakuma <kakuma at valinux co jp>
# Copyright (C) 2014,2015 YAMAMOTO Takashi <yamamoto at valinux co jp>
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

from neutron.plugins.openvswitch.agent import ovs_neutron_agent
from neutron.plugins.openvswitch.agent.ovs_ofctl import br_int
from neutron.plugins.openvswitch.agent.ovs_ofctl import br_phys
from neutron.plugins.openvswitch.agent.ovs_ofctl import br_tun


def init_config():
    pass


def main():
    backend_info = (
        None,
        br_int.OVSIntegrationBridge,
        br_phys.OVSPhysicalBridge,
        br_tun.OVSTunnelBridge,
    )
    ovs_neutron_agent.main(backend_info)
