"""Entry point frozen into the portable LBRDashboard executable.

``LBRDashboard.exe`` starts the dashboard. ``LBRDashboard.exe --self-test
--report <file>`` runs the packaged smoke check instead (see
``lbr_dashboard.self_test``).
"""

import sys

if __name__ == "__main__":
    from lbr_dashboard.self_test import SELF_TEST_FLAG

    if SELF_TEST_FLAG in sys.argv[1:]:
        from lbr_dashboard.self_test import main as self_test

        raise SystemExit(self_test(sys.argv[1:]))

    from lbr_dashboard.app import main

    raise SystemExit(main())
