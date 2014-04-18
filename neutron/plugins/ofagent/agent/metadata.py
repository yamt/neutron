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
#
# @author: YAMAMOTO Takashi, VA Linux Systems Japan K.K.


# metadata mask
NETWORK_MASK = 0xfff
LOCAL = 0x10000  # the packet came from local vm ports


def mk_metadata(network, flags=0):
    return (flags | network, flags | NETWORK_MASK)
