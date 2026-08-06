"""Permissions DRF pour AZLive."""
from __future__ import annotations

import os

from rest_framework.permissions import BasePermission, IsAuthenticated


class IsAuthenticatedOrCronSecret(BasePermission):
    """Auth token vendeur, ou en-tête X-Cron-Secret pour le planificateur."""

    header_name = 'HTTP_X_CRON_SECRET'

    def has_permission(self, request, view) -> bool:
        if IsAuthenticated().has_permission(request, view):
            return True
        expected = os.environ.get('CRON_SECRET', '').strip()
        if not expected:
            return False
        provided = request.META.get(self.header_name, '')
        return bool(provided) and provided == expected
