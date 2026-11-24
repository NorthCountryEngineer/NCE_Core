import base64
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from identity.access import Principal, require_access
from identity.models import AuditEvent, PermissionTag
from platform_services import edge, edge_people
from platform_services.data import publish
from platform_services.models import DataRecord, Dataset, MarketplacePackage, Pipeline
from platform_services.services import install_package, issue_workload_credential
from . import test_edge


JPEG = base64.b64decode('/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAAIAAgDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAb/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFAEBAAAAAAAAAAAAAAAAAAAABf/EABQRAQAAAAAAAAAAAAAAAAAAAAD/2gAMAwEAAhEDEQA/AJkAMYf/2Q==')


def sample(raw=JPEG, identity=None):
    return {'id': str(identity or uuid4()), 'jpeg_base64': base64.b64encode(raw).decode(),
            'sha256': hashlib.sha256(raw).hexdigest(), 'width': 8, 'height': 8}


def observed(identity=None, *, images=True):
    now = timezone.now().isoformat()
    return {'id': str(identity or uuid4()), 'first_seen': now, 'last_seen': now,
            'samples': [sample()] if images else [], 'embeddings': [[1.0]+[0.0]*127],
            'embedding_model': 'sface-v1'}


@override_settings(SECURE_SSL_REDIRECT=False, ISSUER='https://core.example.test', CORE_IOS_TEAM_ID='', CORE_IOS_BUNDLE_ID='ai.nnytech.NCERobotics')
class EdgePeopleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        test_edge.EdgeTests.setUpTestData.__func__(cls)

    def setUp(self):
        test_edge.EdgeTests.setUp(self)

    oauth = test_edge.EdgeTests.oauth

    def api(self, path, data=None, *, token=None, method='POST', raw=None):
        return self.client.generic(method, '/api/edge/v1'+path,
            data=raw if raw is not None else json.dumps(data or {}), content_type='application/json', secure=True,
            HTTP_AUTHORIZATION='Bearer '+(token or self.user_token))

    def upload(self, person, **kwargs):
        return self.api('/agent/people/sync/', {'people': [person]}, token=kwargs.get('token', self.agent_token))

    def test_upload_list_sample_and_agent_page_have_separate_private_shapes(self):
        value = observed()
        result = self.upload(value)
        self.assertEqual(result.status_code, 200, result.content)
        body = result.json()
        self.assertEqual(body['accepted'], [value['id']])
        canonical = body['people'][0]
        self.assertEqual(canonical['name'], 'Unknown-'+value['id'])
        self.assertFalse(canonical['named'])
        self.assertTrue(canonical['face_ready'])
        self.assertEqual(canonical['source_device_id'], str(self.device.pk))
        self.assertEqual(canonical['embeddings'], value['embeddings'])
        self.assertNotIn('jpeg_base64', json.dumps(body))
        listing = self.api('/people/', method='GET')
        self.assertEqual(listing.status_code, 200)
        person = listing.json()['people'][0]
        self.assertEqual(person['id'], value['id'])
        self.assertNotIn('embeddings', person)
        self.assertNotIn('jpeg_base64', json.dumps(person))
        self.assertEqual(listing['Cache-Control'], 'private, no-store')
        photo = self.api(f"/people/{value['id']}/samples/{value['samples'][0]['id']}/", method='GET')
        self.assertEqual(photo.status_code, 200)
        self.assertEqual(photo.content, JPEG)
        self.assertEqual(photo['Content-Type'], 'image/jpeg')
        self.assertEqual(photo['X-Content-SHA256'], hashlib.sha256(JPEG).hexdigest())
        self.assertEqual(photo['Cache-Control'], 'private, no-store')

    def test_idempotent_seen_updates_coalesce_and_preserve_samples(self):
        value = observed()
        self.assertEqual(self.upload(value).status_code, 200)
        record = DataRecord.objects.get(event_id=value['id'])
        original = deepcopy(record.data)
        value['last_seen'] = (timezone.now()+timedelta(minutes=1)).isoformat()
        value.pop('samples')
        self.assertEqual(self.upload(value).status_code, 200)
        record.refresh_from_db()
        self.assertEqual(record.data, original)
        self.assertEqual(DataRecord.objects.filter(dataset=record.dataset).count(), 1)
        with patch('platform_services.edge_people.timezone.now', return_value=timezone.now()+timedelta(hours=2)):
            result = edge_people.sync(self.credential, {'people': [value]})
        self.assertEqual(result['people'][0]['revision'], 2)
        record.refresh_from_db()
        self.assertEqual(len(record.data['samples']), 1)
        self.assertEqual(record.data['last_seen'], value['last_seen'])
        value['embeddings'] = [[1.0]+[1e-8]*127]
        self.assertEqual(self.upload(value).status_code, 200)
        record.refresh_from_db()
        self.assertEqual(record.data['embeddings'], original['embeddings'])

    def test_human_rename_is_idempotent_and_authoritative(self):
        value = observed()
        self.upload(value)
        path = '/people/'+value['id']+'/'
        rename = {'name': 'A known person', 'expected_revision': 1}
        result = self.api(path, rename, method='PATCH')
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(result.json()['person']['revision'], 2)
        self.assertTrue(result.json()['person']['named'])
        self.assertEqual(self.api(path, rename, method='PATCH').json()['person']['revision'], 2)
        self.assertEqual(self.api(path, {'name':'Different', 'expected_revision':1}, method='PATCH').status_code, 409)
        value['legacy_name'] = 'Stale migrated name'
        resynced = self.upload(value)
        self.assertEqual(resynced.status_code, 200)
        self.assertEqual(resynced.json()['people'][0]['name'], 'A known person')
        value['name'] = 'Device overwrite'
        self.assertEqual(self.upload(value).status_code, 400)
        self.assertNotIn('A known person', json.dumps(list(AuditEvent.objects.values('action','target','reason_code'))))

    def test_legacy_name_only_applies_to_first_creation_and_body_candidates_can_enrich(self):
        value = observed(images=False)
        value.update(legacy_name='Earlier human name', embeddings=[])
        created = self.upload(value).json()['people'][0]
        self.assertEqual(created['name'], 'Earlier human name')
        self.assertTrue(created['named'])
        self.assertFalse(created['face_ready'])
        value['embeddings'] = [[1.0]+[0.0]*127]
        value['samples'] = [sample()]
        value['legacy_name'] = 'Not a rename'
        enriched = self.upload(value).json()['people'][0]
        self.assertEqual(enriched['name'], 'Earlier human name')
        self.assertEqual(enriched['revision'], 2)
        self.assertTrue(enriched['face_ready'])

    def test_tenant_isolation_source_tags_and_write_permission(self):
        value = observed()
        self.upload(value)
        other_token = self.oauth(self.admin, self.other_app, self.admin_evidence)
        self.assertEqual(self.api('/people/', token=other_token, method='GET').json()['people'], [])
        photo = f"/people/{value['id']}/samples/{value['samples'][0]['id']}/"
        self.assertEqual(self.api(photo, token=other_token, method='GET').status_code, 404)
        self.assertEqual(self.api('/people/'+value['id']+'/', {'name':'Reader rename','expected_revision':1},
                                  token=self.reader_token, method='PATCH').status_code, 403)
        tag = PermissionTag.objects.create(organization=self.organization, slug='person-sensitive', name='Restricted')
        self.device.permission_tags.add(tag)
        self.assertEqual(self.api('/people/', token=self.reader_token, method='GET').json()['people'], [])
        self.assertEqual(self.api(photo, token=self.reader_token, method='GET').status_code, 403)

    def test_other_device_cannot_overwrite_uuid_and_revocation_blocks_sync(self):
        value = observed()
        self.upload(value)
        device, _, code = edge.create_enrollment(self.installation, self.principal, 'Other robot')
        _, _, token = edge.enroll(code, 'Other robot', '0.1.0', {})
        self.assertEqual(self.upload(value, token=token).status_code, 409)
        self.credential.active = False
        self.credential.save(update_fields=['active'])
        self.assertEqual(self.upload(value).status_code, 401)
        self.credential.active = True
        self.credential.save(update_fields=['active'])
        self.device.active = False
        self.device.save(update_fields=['active'])
        self.assertEqual(self.upload(value).status_code, 401)

    def test_sample_hash_dimensions_limits_and_embeddings_reject_atomically(self):
        value = observed()
        bad = []
        for key, content in (('sha256','0'*64), ('width',9), ('jpeg_base64','not base64')):
            item = deepcopy(value); item['samples'][0][key] = content; bad.append(item)
        too_large = deepcopy(value); too_large['samples'] = [sample(b'x'*(edge_people.MAX_JPEG+1))]; bad.append(too_large)
        huge = bytearray(JPEG); marker=huge.index(b'\xff\xc0'); huge[marker+7:marker+9]=(321).to_bytes(2,'big')
        wrong_size = deepcopy(value); wrong_size['samples'] = [sample(bytes(huge))]; bad.append(wrong_size)
        for vectors in ([[0.0]*128], [[2.0]+[0.0]*127], [[float('nan')]+[0.0]*127], [[1.0]*127], [[True]+[0.0]*127]):
            item=deepcopy(value);item['embeddings']=vectors;bad.append(item)
        future=deepcopy(value);future['last_seen']=(timezone.now()+timedelta(days=1)).isoformat();bad.append(future)
        for item in bad:
            with self.subTest(item=bad.index(item)):
                self.assertEqual(self.upload(item).status_code, 400)
        self.assertEqual(DataRecord.objects.count(), 0)
        request={'people':[observed() for _ in range(6)]}
        self.assertEqual(self.api('/agent/people/sync/', request, token=self.agent_token).status_code, 400)
        self.assertEqual(self.api('/agent/people/sync/', token=self.agent_token, raw=b' '*(edge_people.MAX_REQUEST+1)).status_code, 400)

    def test_samples_are_additive_immutable_and_bounded(self):
        value=observed()
        self.upload(value)
        value['samples']=[sample(),sample()]
        self.assertEqual(self.upload(value).status_code, 200)
        record=DataRecord.objects.get(event_id=value['id'])
        self.assertEqual(len(record.data['samples']), 3)
        value['samples']=[sample()]
        self.assertEqual(self.upload(value).status_code, 400)
        original=record.data['samples'][0]
        replacement=sample(JPEG[:2]+b'\xff\xfe\x00\x03x'+JPEG[2:], original['id'])
        value['samples']=[replacement]
        self.assertEqual(self.upload(value).status_code, 409)
        record.refresh_from_db()
        self.assertEqual(record.data['samples'][0], original)

    def test_pagination_accepted_rows_and_tombstone_cache(self):
        values=[observed(images=False) for _ in range(3)]
        self.assertEqual(self.api('/agent/people/sync/', {'people':values}, token=self.agent_token).status_code, 200)
        first=self.api('/people/?limit=1', method='GET').json()
        second=self.api('/people/?limit=1&after='+str(first['next_cursor']), method='GET').json()
        self.assertNotEqual(first['people'][0]['id'],second['people'][0]['id'])
        path='/people/'+values[0]['id']+'/'
        deletion=self.api(path,{'expected_revision':1},method='DELETE')
        self.assertEqual(deletion.status_code,200)
        tombstone=deletion.json()['person']
        self.assertTrue(tombstone['deleted']);self.assertEqual(tombstone['samples'],[])
        self.assertEqual(self.api(path,{'expected_revision':1},method='DELETE').status_code,200)
        self.assertEqual(len(self.api('/people/',method='GET').json()['people']),2)
        cached=self.upload(values[0]).json()['people'][0]
        self.assertTrue(cached['deleted']);self.assertEqual(cached['embeddings'],[])
        # An accepted person is returned even if its primary-key page was passed.
        record=DataRecord.objects.get(event_id=values[2]['id'])
        result=self.api('/agent/people/sync/',{'people':[values[2]],'cursor':record.pk},token=self.agent_token).json()
        self.assertEqual(result['people'][0]['id'],values[2]['id'])
        for path in ('/people/?limit=51','/people/?after=-1','/people/?after=1&after=2'):
            self.assertEqual(self.api(path,method='GET').status_code,400)

    def test_registry_capacity_includes_tombstones_and_owner_is_immutable(self):
        value=observed(images=False)
        self.upload(value)
        record=DataRecord.objects.get(event_id=value['id'])
        template=record.data
        rows=[]
        for _ in range(edge_people.MAX_PEOPLE-1):
            identity=str(uuid4());data={**template,'id':identity,'deleted':True,'samples':[],'embeddings':[]}
            rows.append(DataRecord(dataset=record.dataset,event_id=identity,data=data))
        DataRecord.objects.bulk_create(rows)
        full=self.upload(observed(images=False))
        self.assertEqual(full.status_code,409)
        self.assertEqual(full.json()['error'],'people_registry_full')
        self.assertEqual(record.dataset.records.count(),500)
        for field,replacement in (('slug','renamed'),('schema',{}),('organization_id',self.other_org.pk)):
            dataset=Dataset.objects.get(pk=record.dataset_id)
            setattr(dataset,field,replacement)
            with self.assertRaises(ValidationError):dataset.save()
        self.assertEqual(Dataset.objects.get(pk=record.dataset_id).slug,edge_people.SLUG)

    def test_reserved_registry_blocks_generic_data_and_federation_access(self):
        value=observed()
        self.upload(value)
        dataset=Dataset.objects.get(slug=edge_people.SLUG)
        with self.assertRaises(PermissionDenied):publish(dataset,'bypass',{})
        package=MarketplacePackage.objects.create(slug='people-data-consumer',name='Data consumer',kind='application',description='Fixture data consumer',
            manifest={'image':'registry.example.test/app@sha256:'+'a'*64,'dependencies':['core-data'],
                      'capabilities':['data.read','data.write']})
        app=install_package(self.organization,package,['data.read','data.write'])
        credential,token=issue_workload_credential(app,'Test data consumer')
        response=self.client.get('/api/platform/v1/datasets/'+edge_people.SLUG+'/records/',
                                 HTTP_AUTHORIZATION='Bearer '+token,secure=True)
        self.assertEqual(response.status_code,403)
        pipeline=Pipeline.objects.create(organization=self.organization,slug='people-bypass',name='Bypass',dataset=dataset)
        response=self.client.post('/api/platform/v1/pipelines/people-bypass/events/',
            json.dumps({'event_id':'bypass','data':{}}),content_type='application/json',
            HTTP_AUTHORIZATION='Bearer '+token,secure=True)
        self.assertEqual(response.status_code,403)
        from core_fabric.models import FabricContract,FabricPeer
        from core_fabric.api import bridge
        peer=FabricPeer.objects.create(organization=self.organization,name='Peer',issuer='https://peer.example.test',
            remote_organization_id=uuid4(),inbound_installation=app,credential_file='/not-read')
        contract=FabricContract.objects.create(organization=self.organization,peer=peer,direction='export',name='Private export',
            dataset=dataset,purpose_code='testing',fields=['samples','embeddings'])
        principal=Principal(credential=credential,purpose='testing')
        request=SimpleNamespace(access_principal=principal,credential=credential,organization=self.organization,
                                headers={'X-Core-Fabric-Issuer':peer.issuer})
        # Even otherwise approved/capable export requests stop at the source access check.
        with patch('core_fabric.api.approved'),patch('core_fabric.api.capability'):
            with self.assertRaises(PermissionDenied):bridge(request,contract)
        with self.assertRaises(PermissionDenied):require_access(self.principal,pipeline,'workflow.execute')
        self.assertEqual(DataRecord.objects.filter(dataset=dataset).count(),1)
