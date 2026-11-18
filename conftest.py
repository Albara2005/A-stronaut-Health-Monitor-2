import pytest

from expert_system import ExpertSystem
from pipeline import Pipeline
from storage import Storage


class FakePublisher:
    def __init__(self):
        self.sent = []

    def publish_decision(self, device_id, summary):
        self.sent.append((device_id, summary))


@pytest.fixture
def pipe():
    p = Pipeline(ExpertSystem(), Storage(":memory:"), FakePublisher())
    yield p
    p.storage.close()
