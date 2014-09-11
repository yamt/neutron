#! /usr/bin/env python

import eventlet


def _run():
    from neutron.tests.functional.agent.linux.helpers import \
        iptables_binary_name

    iptables_binary_name.check_binary_name(argv[1])

if __name__ == "__main__":
    eventlet.spawn(_run).wait()
