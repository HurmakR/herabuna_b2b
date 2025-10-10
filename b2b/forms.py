from django import forms
from django.contrib.auth.forms import UserCreationForm, AuthenticationForm
from .models import Dealer, Address
from django.contrib.auth import authenticate, get_user_model
import re

PHONE_RE = re.compile(r"^380\d{9}$")

def _clean_phone(v):
    return (v or "").replace(" ", "").replace("-", "").replace("(", "").replace(")", "")

class DealerSignUpForm(UserCreationForm):
    email = forms.EmailField(label="Email", required=True)
    first_name = forms.CharField(label="Ім’я", required=False, max_length=150)
    last_name = forms.CharField(label="Прізвище", required=False, max_length=150)
    company_name = forms.CharField(label="Компанія / Магазин", required=False, max_length=255)
    phone = forms.CharField(label="Телефон", required=False, max_length=20)
    telegram_chat_id = forms.CharField(label="Telegram chat ID", required=False, max_length=64)

    class Meta(UserCreationForm.Meta):
        model = Dealer
        fields = ("username", "email", "first_name", "last_name",
                  "company_name", "phone", "telegram_chat_id")

    def clean_phone(self):
        phone = _clean_phone(self.cleaned_data.get("phone"))
        if phone and not PHONE_RE.match(phone):
            raise forms.ValidationError("Введіть номер у форматі 380XXXXXXXXX.")
        return phone


class ProfileForm(forms.ModelForm):
    class Meta:
        model = Dealer
        fields = ("email", "first_name", "last_name", "company_name", "phone", "telegram_chat_id")
        labels = {
            "email": "Email",
            "first_name": "Ім’я",
            "last_name": "Прізвище",
            "company_name": "Компанія / Магазин",
            "phone": "Телефон",
            "telegram_chat_id": "Telegram chat ID",
        }

class AddressForm(forms.ModelForm):
    class Meta:
        model = Address
        fields = [
            "title",
            "city_name", "city_ref",
            "warehouse_name", "warehouse_ref",
            "recipient_name", "recipient_phone",
            "is_default",
        ]
        labels = {
            "title": "Назва адреси (для себе)",
            "city_name": "Місто",
            "warehouse_name": "Відділення Нової Пошти",
            "recipient_name": "Отримувач",
            "recipient_phone": "Телефон отримувача",
            "is_default": "За замовчуванням",
        }
        widgets = {
            "city_ref": forms.HiddenInput(),
            "warehouse_ref": forms.HiddenInput(),
        }

    def clean(self):
        data = super().clean()
        if not data.get("city_ref") or not data.get("warehouse_ref"):
            raise forms.ValidationError("Оберіть місто та відділення зі списку підказок.")


class UAAuthenticationForm(AuthenticationForm):
    error_messages = {
        "invalid_login": "Невірний логін або пароль.",
        "inactive": "Ваш акаунт ще не активовано адміністратором. "
                    "Після підтвердження ви зможете увійти.",
    }

    def clean(self):
        username = self.cleaned_data.get("username")
        password = self.cleaned_data.get("password")
        User = get_user_model()

        if not username or not password:
            raise forms.ValidationError(self.error_messages["invalid_login"], code="invalid_login")

        # 1) Звичайна автентифікація (для активних)
        user = authenticate(self.request, username=username, password=password)
        if user is not None:
            self.user_cache = user
            return self.cleaned_data

        # 2) Перевіряємо кейс "правильний пароль, але неактивний"
        #    authenticate() поверне None, тож шукаємо кандидата і перевіряємо пароль вручну
        candidate = None
        try:
            if "@" in username:
                candidate = User.objects.get(email__iexact=username)
            else:
                candidate = User.objects.get(username__iexact=username)
        except User.DoesNotExist:
            candidate = None

        if candidate and candidate.check_password(password):
            if candidate.is_active is False:
                # Точкове повідомлення для неактивного акаунта
                raise forms.ValidationError(self.error_messages["inactive"], code="inactive")

        # Інакше — стандартна помилка
        raise forms.ValidationError(self.error_messages["invalid_login"], code="invalid_login")