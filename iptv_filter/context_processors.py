from . import __version__


def app_info(request):
    """Available in every template: version for the footer, and the current
    URL name so the sidebar can highlight the active page."""
    match = getattr(request, 'resolver_match', None)
    return {
        'app_version': __version__,
        'active_page': match.url_name if match else '',
    }
