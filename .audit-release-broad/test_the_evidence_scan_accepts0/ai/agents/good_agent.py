def ask(assistant, evidence):
    return assistant.complete_json('sys', untrusted_block('x', evidence))
