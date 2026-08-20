from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models


class UserManager(BaseUserManager):
    """Пользователи заводятся по почте, поля username нет."""

    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError('Почта обязательна')

        user = self.model(email=self.normalize_email(email), **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', False)
        extra_fields.setdefault('is_superuser', False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)

        if extra_fields.get('is_staff') is not True:
            raise ValueError('У суперпользователя is_staff должен быть True')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('У суперпользователя is_superuser должен быть True')

        return self._create_user(email, password, **extra_fields)


class Organization(models.Model):
    """Магазин как юридическое лицо: всё, что заводят сотрудники, принадлежит ему.

    Накладные видит вся организация, а не тот один человек, который их загрузил:
    товар принимает сменщик, а сверяет и отправляет в приёмку хозяин.
    """

    name = models.CharField('название', max_length=255)
    created_at = models.DateTimeField('создана', auto_now_add=True)

    class Meta:
        verbose_name = 'организация'
        verbose_name_plural = 'организации'
        ordering = ('name',)

    def __str__(self):
        return self.name


class User(AbstractUser):
    class Role(models.TextChoices):
        OWNER = 'owner', 'Владелец'
        ADMIN = 'admin', 'Администратор'
        MANAGER = 'manager', 'Менеджер'

    username = None
    email = models.EmailField('почта', unique=True)
    name = models.CharField('имя', max_length=150, blank=True)

    # Пусто только у суперпользователя из консоли: он заходит в админку Django,
    # а не в кабинет, и организации у него нет. Через API без неё не пускаем.
    organization = models.ForeignKey(
        Organization,
        verbose_name='организация',
        on_delete=models.PROTECT,
        related_name='members',
        null=True,
        blank=True,
    )
    role = models.CharField(
        'роль',
        max_length=16,
        choices=Role.choices,
        default=Role.MANAGER,
    )

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    objects = UserManager()

    class Meta:
        verbose_name = 'пользователь'
        verbose_name_plural = 'пользователи'

    def __str__(self):
        return self.email

    @property
    def manages_organization(self) -> bool:
        """Владелец и администратор ведут организацию, менеджер — только работает.

        Пока эта разница видна на расширениях: менеджеру их не показываем, и
        подключать их он не может. Роли людей и настройки организации, когда
        появятся, встанут сюда же.
        """

        return self.role in (self.Role.OWNER, self.Role.ADMIN)
