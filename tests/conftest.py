"""Keep the test suite away from the real .env, so no test sends data to the local Langfuse.

This runs before any test module imports context.config, which would load .env. Empty values also
stop graphiti_core's own load_dotenv() from filling the keys in, since it never overrides a variable
that is already set. Tests that need Langfuse settings set them explicitly.
"""

import os
import tempfile

_empty_env_file = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
_empty_env_file.close()
os.environ["HELIX_ENV_FILE"] = _empty_env_file.name
for _key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL"):
    os.environ[_key] = ""
