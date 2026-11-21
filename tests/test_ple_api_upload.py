"""Exercise real RSA upload/decryption with a synthetic non-provider key."""
import base64
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

PATH=Path(__file__).resolve().parents[1]/'cloud/api_upload.py'
spec=importlib.util.spec_from_file_location('api_upload_fixture',PATH)
upload=importlib.util.module_from_spec(spec);spec.loader.exec_module(upload)


def encrypted(root,request,key=b'synthetic-test-credential'):
    public=root/'fixture-public.pem'
    public.write_text(request['public_key_pem'])
    cipher=upload.openssl('pkeyutl','-encrypt','-pubin','-inkey',str(public),
                         '-pkeyopt','rsa_padding_mode:oaep','-pkeyopt','rsa_oaep_md:sha256',input=key)
    return {'run_id':request['run_id'],'public_key_sha256':request['public_key_sha256'],
            'ciphertext':base64.b64encode(cipher).decode()}


def test_per_run_upload_roundtrip_and_private_key_cleanup(tmp_path):
    request=upload.prepare(tmp_path,'123')
    payload=encrypted(tmp_path,request)
    assert 'synthetic-test-credential' not in json.dumps(payload)
    upload.decrypt(tmp_path,payload)
    env=tmp_path/'model.env'
    assert env.read_bytes()==b'MODEL_API_KEY=synthetic-test-credential\n'
    assert env.stat().st_mode & 0o777==0o600
    assert not (tmp_path/'upload-private.pem').exists()
    assert 'private' not in (tmp_path/'api-request.json').read_text().lower()


def test_upload_for_other_run_cannot_write_a_credential(tmp_path):
    request=upload.prepare(tmp_path,'123')
    payload=encrypted(tmp_path,request);payload['run_id']='456'
    with pytest.raises(ValueError,match='different run'):
        upload.decrypt(tmp_path,payload)
    assert not (tmp_path/'model.env').exists()


def test_encrypted_multiline_credential_is_rejected(tmp_path):
    request=upload.prepare(tmp_path,'123')
    payload=encrypted(tmp_path,request,b'key\nOTHER=bad')
    with pytest.raises(ValueError,match='Invalid model credential'):
        upload.decrypt(tmp_path,payload)
    assert not (tmp_path/'model.env').exists()


def test_ciphertext_from_previous_run_does_not_decrypt(tmp_path):
    first=tmp_path/'first';second=tmp_path/'second'
    request=upload.prepare(first,'123')
    payload=encrypted(first,request)
    new=upload.prepare(second,'456')
    payload.update(run_id='456',public_key_sha256=new['public_key_sha256'])
    with pytest.raises(ValueError,match='encryption operation failed'):
        upload.decrypt(second,payload)
    assert not (second/'model.env').exists()
