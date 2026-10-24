"""Do not mistake mileage followed by driving prose for contact dictation."""
import pytest
from app.listening_cues import listening_gate
from test_listening_cues import SAFE


@pytest.mark.parametrize('mileage', ['60,000', '60000'])
def test_vehicle_description_keeps_listening_at_every_partial(mileage):
    text = f'It has about {mileage} miles and I mainly drive around town during the week'
    words = text.split()
    for end in range(6, len(words)+1):
        assert listening_gate(' '.join(words[:end]), 'Tell me about the vehicle.',
            {**SAFE, 'stage': 'details', 'has_pending_proposal': True}) == 'eligible'


@pytest.mark.parametrize('text', ['123 Main Street', '42 North Valley View Drive',
    '123 Miles Drive', '512 555 0198', 'jordan@example.test'])
def test_real_contact_details_remain_excluded(text):
    assert listening_gate(text, 'Tell me more.', SAFE) == 'private_content'
