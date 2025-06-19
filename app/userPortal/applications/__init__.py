from importlib import import_module

# Initialize the 'applications' package so that nested blueprints can be imported

def _import_blueprints():
    """Dynamically import sub-modules that expose Flask blueprints.

    This allows us to keep the public API of this package limited while
    ensuring that blueprints such as ``jobListing`` are discovered when the
    package is imported elsewhere in the project.
    """
    modules = [
        "app.userPortal.applications.jobListing",  # add additional sub-modules here
    ]

    for module in modules:
        try:
            import_module(module)
        except ModuleNotFoundError:
            # If a module isn't present, we simply skip it. This makes the
            # import process resilient to optional features.
            pass


# Trigger import on package initialization
_import_blueprints() 