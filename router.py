import json
import os
import subprocess

def apply(config, root):
    public_ip = config['public_ip']
    public_host = config['public_host']
    public_interfaces = config['public_interfaces']

    # Check interface uses
    ifaces={}
    for iface in config['interfaces']:
        t = iface['type']
        n = iface['name']
        if t == 'hardware':
            ifaces[iface['name']] = ''
        elif t == 'vlan':
            assert ifaces[iface['parent']] in ('', 'vlan')
            ifaces[iface['parent']] = 'vlan'
            ifaces[iface['name']] = ''
        elif t == 'pppoe':
            assert ifaces[iface['parent']] == ''
            ifaces[iface['parent']] = 'pppoe'
            ifaces[iface['name']] = ''
        else:
            assert False

    for network in config['networks']:
        assert ifaces[network['interface']] == ''
        ifaces[network['interface']] = 'network'

    # Create vlans
    for name, use in ifaces.items():
        if use == 'vlan':
            with open(root+'/etc/systemd/network/{}.network'.format(name), 'wt') as f:
                f.write('[Match]\nName={}\n[Network]\n'.format(name))
                for i in config['interfaces']:
                    if i['type'] == 'vlan' and i['parent'] == name:
                        f.write('VLAN={name}\n'.format(**i))

    for i in config['interfaces']:
        if i['type'] == 'vlan':
            with open(root+'/etc/systemd/network/{}.netdev'.format(i['name']), 'wt') as f:
                f.write('[NetDev]\nName={name}\nKind=vlan\n\n[VLAN]\nId={vlan}\n'.format(**i))

    # Configure ppp
    with open(root+'/etc/systemd/system/ppp@.service', 'wt') as f:
        f.write('''
[Unit]
Description=PPP link to %I
Before=network.target

[Service]
ExecStart=/usr/bin/sh -c 'sleep 1 && /usr/sbin/pppd call %I nodetach nolog'
RestartSec=10
Restart=always

[Install]
WantedBy=multi-user.target
''')

    with open(root+'/etc/ppp/pap-secrets', 'wt') as f:
        for i in config['interfaces']:
            if i['type'] == 'pppoe':
                f.write('{username} * {password}\n'.format(**i))
    with open(root+'/etc/ppp/chap-secrets', 'wt') as f:
        for i in config['interfaces']:
            if i['type'] == 'pppoe':
                f.write('{username} * {password}\n'.format(**i))

    for i in config['interfaces']:
        if i['type'] == 'pppoe':
            with open(root+'/etc/ppp/peers/{name}'.format(**i), 'wt') as f:
                f.write('''plugin rp-pppoe.so
# rp_pppoe_ac 'your ac name'
# rp_pppoe_service 'your service name'
# network interface
{parent}
# login name
name "{username}"
usepeerdns
persist
# Uncomment this if you want to enable dial on demand
#demand
#idle 180
defaultroute
hide-password
noauth
'''.format(**i))


    # build /etc/resolv.conf
    os.remove(root+'/etc/resolv.conf')  # Remove it in case it's a symlink
    with open(root+'/etc/resolv.conf', 'wt') as f:
        for ns in config['nameservers']:
            f.write('nameserver {}\n'.format(ns))

    # Build udev rules for interface naming
    with open(root+'/etc/udev/rules.d/10-router.rules', 'wt') as f:
        for iface in config['interfaces']:
            if iface['type'] == 'hardware':
                f.write('SUBSYSTEM=="net", ACTION=="add", ATTR{{address}}=="{mac}", NAME="{name}"\n'.format(**iface))


    # Build network interface files
    for network in config['networks']:
        with open(root+'/etc/systemd/network/{}.network'.format(network['interface']), 'wt') as f:
            f.write('''
[Match]
Name={interface}

[Network]
IPForward=yes

[Address]
Address={ip}/{range}
'''.format(**network))

    # Build hosts file
    with open(root+'/etc/hosts', 'wt') as f:
        f.write('127.0.0.1	localhost.localdomain	localhost\n')
        f.write('::1		localhost.localdomain	localhost\n')
        f.write('{} {}\n'.format(public_ip, public_host))

        for network in config['networks']:
            for host in network['hosts']:
                if host.get('name'):
                    f.write("{ip} {name}\n".format(**host))

    # Build dnsmasq config
    with open(root+'/etc/dnsmasq.conf', 'wt') as f:
        for network in config['networks']:
            if network.get('dhcp'):
                f.write("dhcp-range={start},{end},1h\n".format(**network['dhcp']))
            for host in network['hosts']:
                if host.get('mac'):
                    f.write("dhcp-host={mac},{ip}\n".format(**host))


    with open(root+'/etc/iptables/iptables.rules', 'wt') as f:

        flt = ''
        nat = ''

        for network in config['networks']:
            for host in network['hosts']:
                if host.get('ports'):
                    for port in host['ports']:
                        flt += '-A fw-open -d {host[ip]} -p {port[proto]} -m {port[proto]} --dport {port[source]} -j ACCEPT\n'.format(host=host, port=port)
                        nat += '-A INCOMING -p {port[proto]} -m {port[proto]} --dport {port[source]} -j DNAT --to-destination {host[ip]}:{port[dest]}\n'.format(host=host, port=port)
                        nat += '-A POSTROUTING -d {host[ip]} -s {network[ip]}/{network[range]} -p {port[proto]} --dport {port[dest]} -j SNAT --to {network[ip]}\n'.format(network=network, host=host, port=port)

        with open('iptables-filter.rules', 'r') as tpl:
            f.write(tpl.read())
        f.write(flt)
        for network in config['networks']:
            f.write('-A fw-interfaces -i {network[interface]} -s {network[ip]}/{network[range]} -j ACCEPT\n'.format(network=network))

        f.write("COMMIT\n")
        with open('iptables-nat.rules', 'r') as tpl:
            f.write(tpl.read())

        for network in config['networks']:
            for public_if in public_interfaces:
                f.write('-A POSTROUTING -s {network[ip]}/{network[range]} -o {public_if} -j MASQUERADE\n'.format(network=network, public_if=public_if))
            f.write('-A PREROUTING -d {public_ip} -s {network[ip]}/{network[range]} -j INCOMING\n'.format(public_ip=public_ip, network=network))

        for public_if in public_interfaces:
            f.write('-A PREROUTING -i {public_if} -j INCOMING\n'.format(public_if=public_if))

        f.write(nat)
        f.write("COMMIT\n")

if __name__ == "__main__":
    with open('config.json') as f:
        config = json.load(f)

    apply(config, '')
    subprocess.call('systemctl restart iptables', shell=True)
    subprocess.call('systemctl restart dnsmasq', shell=True)
