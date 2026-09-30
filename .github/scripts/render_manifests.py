"""Render script Jobs without accessing a cluster."""
import argparse
import hashlib
import json
import yaml
from pathlib import Path
import re


class ManifestDumper(yaml.SafeDumper):
    """Keep multiline NDN source readable as a YAML block scalar."""


def represent_string(dumper, value):
    return dumper.represent_scalar('tag:yaml.org,2002:str', value,
                                   style='|' if '\n' in value else None)


ManifestDumper.add_representer(str, represent_string)


def load_function(path):
    """Read the small function schema used by registration Jobs."""
    data = yaml.safe_load(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or set(data) - {'function', 'preference'}:
        raise ValueError(f'{path}: expected function and optional preference mappings')
    function = data.get('function')
    if not isinstance(function, dict) or set(function) != {'name', 'source'}:
        raise ValueError(f'{path}: function must contain exactly name and source')
    name, source = function['name'], function['source']
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', name):
        raise ValueError(f'{path}: function.name must be a nonempty single name (letters, digits, _, ., -)')
    if not isinstance(source, str) or not source.strip() or '\x00' in source:
        raise ValueError(f'{path}: function.source must be nonempty text without NUL characters')
    preference = data.get('preference', {})
    if not isinstance(preference, dict) or set(preference) - {'cpu', 'memory'}:
        raise ValueError(f'{path}: preference supports only cpu and memory')
    preference = {key: preference.get(key, 'medium') for key in ('cpu', 'memory')}
    for key, value in preference.items():
        if not isinstance(value, str) or value not in ('high', 'medium', 'low'):
            raise ValueError(f'{path}: preference.{key} must be high, medium, or low')
    return name, source, preference


def render(scripts, mode):
    folder, executable, wave = (
        ('func', 'register', '1') if mode == 'register'
        else ('use-func', 'invoke', '2')
    )
    image = 'registerer' if mode == 'register' else 'invoker'
    items = []
    if mode == 'register':
        if any((scripts / folder).rglob('*.ndn')):
            raise ValueError('Migrate func/*.ndn to YAML with function.name and function.source')
        paths = sorted(p for p in (scripts / folder).rglob('*')
                       if p.is_file() and p.suffix in ('.yaml', '.yml'))
    else:
        paths = sorted((scripts / folder).rglob('*.ndn'))
    names = set()
    for path in paths:
        if mode == 'register':
            function_name, code, preference = load_function(path)
            if function_name in names:
                raise ValueError(f'Duplicate function.name: {function_name}')
            names.add(function_name)
            # A preference-only edit must also create a new immutable Job.
            identity = json.dumps([function_name, code, preference], sort_keys=True)
            slug_source = function_name
            version = 'function-yaml-v1'
        else:
            if not re.fullmatch(r'[A-Za-z0-9_.-]+', path.name):
                raise ValueError(f'Unsupported script filename: {path.name}')
            code = path.read_text(encoding='utf-8')
            if not code.strip() or '\x00' in code:
                raise ValueError(f'Script must be nonempty text without NUL characters: {path}')
            relative = path.relative_to(scripts).as_posix()
            identity = relative + '\0' + code
            slug_source = path.stem
            version = 'nlsr-service-v1'
        digest = hashlib.sha256((version + identity).encode()).hexdigest()[:12]
        slug = re.sub('[^a-z0-9-]', '-', slug_source.lower()).strip('-')[:25] or 'script'
        name = f'function-{mode}-{slug}-{digest}'
        metadata = {'name': name, 'namespace': 'ndn'}
        env = [{'name': 'NFD_CONFIG_PATH', 'value': '/etc/ndn-config/ADDRESS'}]
        # Kubernetes expands $(VAR) in env values; double dollars preserve code literally.
        source_value = code.replace('$', '$$')
        if mode == 'register':
            env.extend([
                {'name': 'FUNCTION_NAME', 'value': function_name},
                {'name': 'FUNCTION_CODE', 'value': source_value},
                {'name': 'FUNCTION_PREFERENCE_CPU', 'value': preference['cpu']},
                {'name': 'FUNCTION_PREFERENCE_MEMORY', 'value': preference['memory']},
                {'name': 'MANAGER_REGISTER_NAME', 'value': '/Manager/register'},
            ])
        else:
            env.extend([
                {'name': 'NDN_SCRIPT_NAME', 'value': path.name},
                {'name': 'NDN_SCRIPT_SOURCE', 'value': source_value},
            ])
        container = {
            'name': image,
            'image': f'ghcr.io/nakazato-lab/function-utils/ndn-function-{image}:latest',
            'imagePullPolicy': 'Always',
            'command': ['python', '-u', f'/app/{executable}.py'], 'env': env,
            'volumeMounts': [
                {'name': 'nfd-endpoint', 'mountPath': '/etc/ndn-config', 'readOnly': True}],
        }
        items.append({'apiVersion': 'batch/v1', 'kind': 'Job',
                      'metadata': {**metadata, 'annotations': {'argocd.argoproj.io/sync-wave': wave}},
                      'spec': {'backoffLimit': 2, 'activeDeadlineSeconds': 180 if mode == 'register' else 300,
                               'template': {'metadata': {'labels': {'app': f'function-{mode}'}},
                                            'spec': {'restartPolicy': 'Never', 'containers': [container],
                                                     'volumes': [
                                                         {'name': 'nfd-endpoint', 'configMap': {'name': 'nfd-client-config'}}]}}}})
    return items


def deletion_jobs(previous, current):
    """Keep DELETE Jobs until re-registration so Argo self-heal does not repeat them."""
    current_names = {
        env['value'] for item in current if item.get('kind') == 'Job'
        for env in item['spec']['template']['spec']['containers'][0]['env']
        if env['name'] == 'FUNCTION_NAME'
    }
    result = []
    for job in previous:
        if not job or job.get('kind') != 'Job':
            continue
        spec = job['spec']['template']['spec']
        container = spec['containers'][0]
        env = {item['name']: item['value'] for item in container['env']}
        function_name = env['FUNCTION_NAME']
        if function_name in current_names:
            continue
        # This function name is no longer present in the current definitions.
        if env.get('FUNCTION_OPERATION') != 'DELETE':
            old_name = job['metadata']['name']
            job['metadata']['name'] = old_name.replace('function-register-', 'function-delete-', 1)
            container['env'] = [item for item in container['env'] if item['name'] not in ('FUNCTION_CODE', 'FUNCTION_PREFERENCE_CPU', 'FUNCTION_PREFERENCE_MEMORY')]
            container['env'].append({'name': 'FUNCTION_OPERATION', 'value': 'DELETE'})
            job['spec']['activeDeadlineSeconds'] = 300
            # DELETE needs only the function name, not the old source ConfigMap.
            container['volumeMounts'] = [v for v in container['volumeMounts'] if v['name'] != 'script']
            spec['volumes'] = [v for v in spec['volumes'] if v['name'] != 'script']
        # A Job's Pod template is immutable: changing its endpoint needs a new name.
        for volume in spec.get('volumes', []):
            config_map = volume.get('configMap', {})
            if config_map.get('name') == 'nfd-config':
                config_map['name'] = 'nfd-client-config'
                old_name = job['metadata']['name']
                digest = hashlib.sha256(('nlsr-service-v1' + old_name).encode()).hexdigest()[:12]
                job['metadata']['name'] = old_name.rsplit('-', 1)[0] + '-' + digest
        result.append(job)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scripts', type=Path, required=True)
    parser.add_argument('--manifest-dir', type=Path, required=True)
    args = parser.parse_args()
    # Render both before writing so validation failures do not leave partial output.
    rendered = {mode: render(args.scripts, mode) for mode in ('register', 'invoke')}
    previous_path = args.manifest_dir / 'function-register.yaml'
    if previous_path.exists():
        previous = list(yaml.safe_load_all(previous_path.read_text(encoding='utf-8')))
        rendered['register'].extend(deletion_jobs(previous, rendered['register']))
    for mode, items in rendered.items():
        # An empty List lets Kustomize handle an empty directory.
        documents = items or [{'apiVersion': 'v1', 'kind': 'List', 'items': []}]
        text = '## Auto Generated by function-utils/.github/scripts/render_manifests.py\n'
        text += yaml.dump_all(documents, Dumper=ManifestDumper,
                              allow_unicode=True, sort_keys=False)
        (args.manifest_dir / f'function-{mode}.yaml').write_text(text + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
