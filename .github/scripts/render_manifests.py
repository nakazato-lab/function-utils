"""Render script Jobs without accessing a cluster."""
import argparse
import hashlib
import yaml
from pathlib import Path
import re


class ManifestDumper(yaml.SafeDumper):
    """Keep multiline NDN source readable as a YAML block scalar."""


def represent_string(dumper, value):
    return dumper.represent_scalar('tag:yaml.org,2002:str', value,
                                   style='|' if '\n' in value else None)


ManifestDumper.add_representer(str, represent_string)


def render(scripts, mode):
    folder, executable, wave = (
        ('func', 'register', '1') if mode == 'register'
        else ('use-func', 'invoke', '2')
    )
    image = 'registerer' if mode == 'register' else 'invoker'
    items = []
    paths = sorted((scripts / folder).rglob('*.ndn'))
    if mode == 'register':
        names = [p.stem for p in paths]
        if len(names) != len(set(names)):
            raise ValueError('Function filenames must be unique: their stems become NDN prefixes')
    for path in paths:
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', path.name):
            raise ValueError(f'Unsupported script filename: {path.name}')
        code = path.read_text(encoding='utf-8')
        if not code.strip() or '\x00' in code:
            raise ValueError(f'Script must be nonempty text without NUL characters: {path}')
        relative = path.relative_to(scripts).as_posix()
        version = 'inline-code-v1'
        digest = hashlib.sha256((version + relative + '\0' + code).encode()).hexdigest()[:12]
        slug = re.sub('[^a-z0-9-]', '-', path.stem.lower()).strip('-')[:25] or 'script'
        name = f'function-{mode}-{slug}-{digest}'
        metadata = {'name': name, 'namespace': 'ndn'}
        env = [{'name': 'NFD_CONFIG_PATH', 'value': '/etc/ndn-config/ADDRESS'}]
        # Kubernetes expands $(VAR) in env values; double dollars preserve code literally.
        source_value = code.replace('$', '$$')
        if mode == 'register':
            env.extend([
                {'name': 'FUNCTION_NAME', 'value': path.stem},
                {'name': 'FUNCTION_CODE', 'value': source_value},
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
                                                         {'name': 'nfd-endpoint', 'configMap': {'name': 'nfd-config'}}]}}}})
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
        # The .ndn file associated with this Job is already confirmed to be absent from current.
        if env.get('FUNCTION_OPERATION') != 'DELETE':
            old_name = job['metadata']['name']
            job['metadata']['name'] = old_name.replace('function-register-', 'function-delete-', 1)
            container['env'] = [item for item in container['env'] if item['name'] != 'FUNCTION_CODE']
            container['env'].append({'name': 'FUNCTION_OPERATION', 'value': 'DELETE'})
            job['spec']['activeDeadlineSeconds'] = 300
            # DELETE needs only the function name, not the old source ConfigMap.
            container['volumeMounts'] = [v for v in container['volumeMounts'] if v['name'] != 'script']
            spec['volumes'] = [v for v in spec['volumes'] if v['name'] != 'script']
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
