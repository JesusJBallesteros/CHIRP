"""Single place the version is written down, imported by every entry point.

Kept in its own module so nothing has to import a heavy one just to ask. The
git tag and this string are meant to move together: tag v1.3.0, bump here.
"""

__version__ = "1.3.0"
