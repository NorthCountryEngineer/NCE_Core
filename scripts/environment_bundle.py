"""Render an environment namespace and network boundaries; never contact a cluster."""
import argparse
import ipaddress
import json
import re
import uuid


def bundle(namespace, organization_id, environment, core_cidr, core_port=443, ingress_namespace='ingress-nginx'):
    for value in [namespace, ingress_namespace]:
        if not isinstance(value, str) or len(value) > 63 or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?', value):
            raise ValueError('Use Kubernetes DNS labels for namespaces.')
    organization_id = str(uuid.UUID(organization_id))
    if environment not in {'sandbox', 'development', 'staging', 'uat', 'production'}:
        raise ValueError('Use a registered deployment environment name.')
    network = ipaddress.ip_network(core_cidr, strict=True)
    if network.prefixlen != network.max_prefixlen or type(core_port) is not int or not 1 <= core_port <= 65535:
        raise ValueError('Allow exactly one Core endpoint IP (/32 or /128) and its explicit port.')
    labels = {'core.platform/organization': organization_id, 'core.platform/environment': environment}
    metadata = lambda name: {'name': name, 'namespace': namespace, 'labels': labels}
    return {'apiVersion': 'v1', 'kind': 'List', 'items': [
        {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': namespace, 'labels': {
            **labels, 'pod-security.kubernetes.io/enforce': 'restricted', 'pod-security.kubernetes.io/enforce-version': 'v1.31'}}},
        {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy', 'metadata': metadata('core-environment-deny'),
            'spec': {'podSelector': {}, 'policyTypes': ['Ingress', 'Egress'], 'ingress': [], 'egress': []}},
        {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy', 'metadata': metadata('core-environment-allow'),
            'spec': {'podSelector': {}, 'policyTypes': ['Ingress', 'Egress'],
                'ingress': [{'from': [{'podSelector': {}}]}, {'from': [{'namespaceSelector': {
                    'matchLabels': {'kubernetes.io/metadata.name': ingress_namespace}}}]}],
                'egress': [{'to': [{'podSelector': {}}]}, {'to': [{'namespaceSelector': {
                    'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}}, 'podSelector': {
                        'matchLabels': {'k8s-app': 'kube-dns'}}}], 'ports': [{'protocol': protocol, 'port': 53} for protocol in ['UDP', 'TCP']]},
                    {'to': [{'ipBlock': {'cidr': str(network)}}], 'ports': [{'protocol': 'TCP', 'port': core_port}]}]}}
    ]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--organization-id', required=True)
    parser.add_argument('--environment', required=True)
    parser.add_argument('--core-cidr', required=True, help='Exact Core endpoint IP as /32 or /128. Review CNI NAT handling.')
    parser.add_argument('--core-port', type=int, default=443)
    parser.add_argument('--ingress-namespace', default='ingress-nginx')
    args = parser.parse_args()
    try:
        print(json.dumps(bundle(args.namespace, args.organization_id, args.environment, args.core_cidr, args.core_port, args.ingress_namespace)))
    except ValueError as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
