"""Backend plugin package for Bodhi Update Manager.

Each module in this package exposes one concrete 'UpdateBackend' subclass.
Modules are discovered and imported dynamically by 'backends.discover_plugins()';
no explicit import list is maintained here.
"""
