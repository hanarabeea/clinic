from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.core.validators import RegexValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

USERNAME_RE = r"^[\w.\-]+\Z"  # letters (Arabic too), digits, . _ - — never "@", so it can't look like an email


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, username, email, password, **extra):
        email = self.normalize_email(email).lower() if email else None
        username = (username or email or "").strip().lower()
        if not username:
            raise ValueError("A username or an email is required")
        user = self.model(username=username, email=email, **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, username=None, email=None, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(username, email, password, **extra)

    def create_superuser(self, username=None, email=None, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        return self._create_user(username, email, password, **extra)

    def get_by_natural_key(self, username):
        return self.get(username__iexact=username)


class User(AbstractUser):
    """Signs in with the username **or** the email (accounts.backends). Every user has a username; the email is
    optional (users created before 2026-10-25 got their email as username)."""

    username = models.CharField(
        _("اسم المستخدم"),
        max_length=150,
        unique=True,
        validators=[RegexValidator(USERNAME_RE, _("حروف وأرقام و . _ - بس، من غير مسافات ولا @."))],
    )
    first_name = None
    last_name = None
    email = models.EmailField(_("الإيميل"), unique=True, null=True, blank=True)
    full_name = models.CharField(_("الاسم"), max_length=150)
    phone = models.CharField(_("الموبايل"), max_length=20, blank=True)

    USERNAME_FIELD = "username"
    REQUIRED_FIELDS = ["full_name"]

    objects = UserManager()

    class Meta:
        verbose_name = _("مستخدم")
        verbose_name_plural = _("المستخدمين")

    def __str__(self):
        return self.full_name or self.username

    def save(self, *args, **kwargs):
        self.username = (self.username or "").strip().lower()
        self.email = (self.email or "").strip().lower() or None  # "" would clash with the unique constraint
        super().save(*args, **kwargs)

    def get_full_name(self):
        return self.full_name

    def get_short_name(self):
        return self.full_name.split(" ")[0] if self.full_name else self.username
