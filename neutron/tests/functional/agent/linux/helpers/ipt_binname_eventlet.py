#! /usr/bin/env python

import eventlet


def _run():
    from neutron.tests.functional.agent.linux.helpers import ipt_binname

    ipt_binname.print_binary_name()

if __name__ == "__main__":
    eventlet.spawn(_run).wait()
