import faulthandler, sys
faulthandler.enable(file=sys.stderr)
faulthandler.dump_traceback_later(12, repeat=True, file=sys.stderr)
