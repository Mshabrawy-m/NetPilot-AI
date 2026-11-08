class ExactSummary:
    def pytest_terminal_summary(self, terminalreporter, exitstatus, config):
        stats = terminalreporter.stats
        passed = len(stats.get("passed", []))
        failed = len(stats.get("failed", []))
        errors = len(stats.get("error", []))
        skipped = len(stats.get("skipped", []))
        deselected = len(stats.get("deselected", []))
        total = passed + failed + errors + skipped
        terminalreporter.write_sep("=", f"EXACT_FINAL_SUMMARY collected={total + deselected} passed={passed} failed={failed} skipped={skipped} errors={errors} deselected={deselected} exit_status={exitstatus}")
