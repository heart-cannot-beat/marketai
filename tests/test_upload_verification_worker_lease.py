"""The real Worker call site must respect PostgreSQL's lease width."""
import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock, patch, PropertyMock
import pytest
from app.worker_runtime import WorkerRuntime
from app.adapters.persistence.models import UploadIntentRecord


@pytest.mark.parametrize('owner',['short-worker','happydeMacBook-Pro.local:13510:7b979816','long-host-'+'x'*240])
def test_upload_verification_lease_fits_the_persisted_column_for_any_worker_owner(owner):
    runtime=object.__new__(WorkerRuntime);runtime.owner=owner;runtime.blob_store=Mock();runtime.admission_reader=Mock()
    ctx=SimpleNamespace(executor='system:worker')
    job=SimpleNamespace(context=ctx,id='job_12345678901234567890123456789012',payload={'intent_id':'upload','operation_id':'operation'})
    @contextmanager
    def session(_):yield Mock()
    async def verify(context,intent_id,**kwargs):
        maximum=UploadIntentRecord.__table__.c.verification_lease_id.type.length
        assert len(kwargs['lease_id']) <= maximum
        return SimpleNamespace(kind='finalized',committed=SimpleNamespace(committed_key='committed/ws/reference/test.png'))
    with patch('app.worker_runtime.tenant_session',session),patch('app.worker_runtime.get_operation',return_value=SimpleNamespace(state='queued')),\
         patch('app.worker_runtime.transition_operation'),patch('app.worker_runtime.jobs.mark_running'),patch('app.worker_runtime.jobs.succeed'),\
         patch('app.worker_runtime.upload_intents.verify_upload_intent',side_effect=verify):
        asyncio.run(runtime._verify_upload_intent(job))
