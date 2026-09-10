"""Manually (re)download the OmniParser weights.

The same check runs automatically on first use of the vision engine
(see utils/model_setup.ensure_omniparser_weights); this script is just a
convenient entry point for provisioning them ahead of time.
"""

import os
import sys
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.model_setup import ensure_omniparser_weights, OMNIPARSER_WEIGHTS_DIR  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


def download_weights():
    return ensure_omniparser_weights(OMNIPARSER_WEIGHTS_DIR)


if __name__ == "__main__":
    download_weights()
