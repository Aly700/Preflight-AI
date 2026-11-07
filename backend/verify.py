"""The claims contract: the `final_answer` tool the model calls last, carrying claims only."""

from typing import Any

FINAL_ANSWER_TOOL: dict[str, Any] = {
    'name': 'final_answer',
    'description': (
        'Declare the claims in the answer you have already written as text: every record ID you cited '
        'and every number you stated, each number with the id of the tool call and the JSON path (dotted '
        'keys, [n] for list items) it came from. This tool carries no answer text. Call it exactly once, '
        'after the answer text, as the last thing you do. Claims that cannot be verified are shown to the '
        'reader as unverified.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'cited_ids': {
                'type': 'array',
                'description': 'Every record ID that appears in the answer text',
                'items': {'type': 'string'},
            },
            'numbers': {
                'type': 'array',
                'description': 'Every count or quantity stated in the answer text',
                'items': {
                    'type': 'object',
                    'properties': {
                        'label': {'type': 'string', 'description': 'What the number is, in a few words'},
                        'value': {'type': 'number'},
                        'call_id': {'type': 'string', 'description': 'The id of the tool call the number came from'},
                        'path': {
                            'type': 'string',
                            'description': "JSON path inside that result, e.g. 'total' or 'items[0].issue_id'",
                        },
                    },
                    'required': ['label', 'value', 'call_id', 'path'],
                },
            },
        },
        'required': ['cited_ids', 'numbers'],
    },
}
