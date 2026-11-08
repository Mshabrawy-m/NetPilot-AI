def ask(assistant, evidence):
    return assistant.complete_json('sys', str(evidence))
