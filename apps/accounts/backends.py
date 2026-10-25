from django.contrib.auth.backends import ModelBackend
from django.db.models import Q

from .models import User


class UsernameOrEmailBackend(ModelBackend):
    """The login box takes the username or the email (case-insensitive). A username can't contain "@", so the two
    never collide."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        login = (username or kwargs.get(User.USERNAME_FIELD) or "").strip()
        if not login or password is None:
            return None
        user = User.objects.filter(Q(username__iexact=login) | Q(email__iexact=login)).first()
        if user is None:
            User().set_password(password)  # same work as a real check: no timing hint that the account doesn't exist
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
