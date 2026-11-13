"""Regression tests for immutable Job templates and content-based names."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from render_manifests import deletion_jobs, job_name, render


class JobNamingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.scripts = Path(self.directory.name)
        for folder in ('yaml', 'func', 'use-func'):
            (self.scripts / folder).mkdir()
        (self.scripts / 'func/image.ndn').write_text('put_image("sample.png")\n')
        (self.scripts / 'func/sample.png').write_bytes(b'original image')
        (self.scripts / 'yaml/image.yaml').write_text(
            'function:\n  name: image\n  runtime: ndn\n'
            '  source: image.ndn\n  files: [sample.png]\n')
        (self.scripts / 'yaml/plain.yaml').write_text(
            'function:\n  name: plain\n  runtime: ndn\n  source: image.ndn\n')
        (self.scripts / 'use-func/use_image.ndn').write_text('return interest "/image"\n')

    def registration_jobs(self, commit):
        with patch('render_manifests.subprocess.check_output', return_value=commit):
            return render(self.scripts, 'register')

    def test_commit_change_renames_only_jobs_that_fetch_files(self):
        before = self.registration_jobs('a' * 40)
        after = self.registration_jobs('b' * 40)
        self.assertNotEqual(before[0]['spec']['template'], after[0]['spec']['template'])
        self.assertNotEqual(before[0]['metadata']['name'], after[0]['metadata']['name'])
        self.assertEqual(before[1], after[1])

    def test_same_inputs_are_stable(self):
        self.assertEqual(self.registration_jobs('a' * 40), self.registration_jobs('a' * 40))
        self.assertEqual(render(self.scripts, 'invoke'), render(self.scripts, 'invoke'))

    def test_attachment_change_renames_job_even_with_same_commit(self):
        before = self.registration_jobs('a' * 40)[0]
        (self.scripts / 'func/sample.png').write_bytes(b'changed image')
        after = self.registration_jobs('a' * 40)[0]
        self.assertEqual(before['spec']['template'], after['spec']['template'])
        self.assertNotEqual(before['metadata']['name'], after['metadata']['name'])

    def test_pod_configuration_changes_rename_both_job_types(self):
        jobs = [self.registration_jobs('a' * 40)[0], render(self.scripts, 'invoke')[1]]
        for mode, job in zip(('register', 'invoke'), jobs):
            template = job['spec']['template']
            original = job_name(mode, 'image', 'same content', template)
            for container_type in ('containers', 'initContainers'):
                with self.subTest(mode=mode, container_type=container_type):
                    changed = copy.deepcopy(template)
                    changed['spec'][container_type][0]['image'] = 'example/new-image:v2'
                    self.assertNotEqual(original, job_name(mode, 'image', 'same content', changed))
            reordered = dict(reversed(list(template.items())))
            self.assertEqual(original, job_name(mode, 'image', 'same content', reordered))

    def test_template_rename_does_not_generate_function_deletion(self):
        before = self.registration_jobs('a' * 40)
        after = self.registration_jobs('b' * 40)
        self.assertEqual(deletion_jobs(before, after), [])


if __name__ == '__main__':
    unittest.main()
