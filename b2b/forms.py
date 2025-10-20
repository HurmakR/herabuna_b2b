# b2b/forms.py
from django import forms
from django.core.validators import RegexValidator
from django.contrib.auth import get_user_model
from .models import Address
from django.contrib.auth.forms import AuthenticationForm

Dealer = get_user_model()

# 380 + 9 цифр
phone_validator = RegexValidator(
    regex=r"^380\d{9}$",
    message="Введіть номер у форматі 380XXXXXXXXX.",
)

# -------- Signup --------
class DealerSignUpForm(forms.ModelForm):
    password1 = forms.CharField(label="Пароль", widget=forms.PasswordInput, min_length=8)
    password2 = forms.CharField(label="Підтвердіть пароль", widget=forms.PasswordInput)
    phone = forms.CharField(
        label="Телефон",
        max_length=12,
        validators=[phone_validator],
        help_text="Формат: 380XXXXXXXXX",
        widget=forms.TextInput(attrs={
            "placeholder": "380XXXXXXXXX",
            "inputmode": "numeric",
            "pattern": r"380\d{9}",
        }),
    )
    class Meta:
        model = Dealer
        fields = ["username", "email", "company_name", "phone", "first_name", "last_name"]
        labels = {
            "username": "Логін", "email": "Email",
            "company_name": "Компанія / Магазин", "first_name": "Ім’я", "last_name": "Прізвище",
        }

    def clean(self):
        cleaned = super().clean()
        p1, p2 = cleaned.get("password1"), cleaned.get("password2")
        if p1 and p2 and p1 != p2:
            self.add_error("password2", "Паролі не співпадають.")
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password1"])
        if commit:
            user.save()
        return user


# -------- Profile (редагування користувача) --------
class ProfileForm(forms.ModelForm):
    phone = forms.CharField(
        label="Телефон",
        max_length=12,
        validators=[phone_validator],
        widget=forms.TextInput(attrs={
            "placeholder": "380XXXXXXXXX",
            "inputmode": "numeric",
            "pattern": r"380\d{9}",
        }),
    )

    class Meta:
        model = Dealer
        fields = ["first_name", "last_name", "company_name", "email", "phone"]
        labels = {
            "first_name": "Ім’я", "last_name": "Прізвище",
            "company_name": "Компанія / Магазин", "email": "Email", "phone": "Телефон",
        }


# -------- Адреси доставки (Нова Пошта) --------
class AddressForm(forms.ModelForm):
    recipient_phone = forms.CharField(
        label="Телефон отримувача",
        max_length=12,
        validators=[phone_validator],
        widget=forms.TextInput(attrs={
            "placeholder": "380XXXXXXXXX",
            "inputmode": "numeric",
            "pattern": r"380\d{9}",
        }),
    )

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
            "title": "Назва адреси",
            "city_name": "Місто", "warehouse_name": "Відділення",
            "recipient_name": "Отримувач", "recipient_phone": "Телефон отримувача",
            "is_default": "За замовчуванням",
        }
        widgets = {
            "city_name": forms.TextInput(attrs={"data-np-city": "1", "autocomplete": "off"}),
            "warehouse_name": forms.TextInput(attrs={"data-np-warehouse": "1", "autocomplete": "off"}),
        }


class CustomAuthenticationForm(AuthenticationForm):
    error_messages = {
        "invalid_login": "Невірний логін або пароль.",
        "inactive": "Ваш акаунт ще не активовано. Дочекайтеся підтвердження від адміністратора.",
    }