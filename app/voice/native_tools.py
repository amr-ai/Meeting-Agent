"""Keep SDK async bookkeeping out of Gemini's server-issued function identities."""
from livekit.agents import llm

NATIVE_TOOL_REPLY = 'meeting_native_tool_reply'


def native_tool_context(context: llm.ChatContext) -> llm.ChatContext:
    bindings = {}
    for item in context.items:
        if item.type != 'function_call':
            continue
        data = item.extra.get(NATIVE_TOOL_REPLY)
        if (isinstance(data, dict) and isinstance(data.get('root'), str)
                and item.call_id in {data['root'], data['root'] + '_final'}):
            bindings[item.call_id] = data
    if not bindings:
        return context
    # The model's original call and the voice executor's copy are different
    # objects. A marked final also identifies its unmarked original on re-sync.
    for call_id, data in list(bindings.items()):
        if call_id == data['root'] + '_final':
            bindings[data['root']] = data
    completed = {bindings[item.call_id]['root']: item.id for item in context.items
                 if item.type == 'function_call_output' and item.call_id in bindings
                 and item.call_id == bindings[item.call_id]['root'] + '_final'}
    items = []
    for item in context.items:
        data = bindings.get(getattr(item, 'call_id', None))
        if not data:
            items.append(item)
            continue
        if item.type == 'function_call':
            if item.call_id == data['root']:
                binding = {**data, 'completed_output_id': completed.get(data['root'], data.get('completed_output_id'))}
                items.append(item.model_copy(update={'extra': {**item.extra, NATIVE_TOOL_REPLY: binding}}))
        elif item.type == 'function_call_output':
            if (item.call_id == data['root'] + '_final'
                    or item.id == data.get('completed_output_id')):
                items.append(item.model_copy(update={'call_id': data['root'],
                    'reply_required': bool(data.get('reply')) and not data.get('image_question')}))
            # A progress update releases the SDK executor only. The provider's
            # NON_BLOCKING call stays open until its one real, correctly-bound result.
        else:
            items.append(item)
    return llm.ChatContext(items)
