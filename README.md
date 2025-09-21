# Herabuna B2B (Django MVP)

Мінімальний B2B портал для дилерів:
- Реєстрація/логін
- Каталог товарів з гуртовими цінами
- Формування замовлення (draft → submitted)
- Друк рахунку та накладної (HTML під друк)
- Команда синхронізації з WooCommerce: `python manage.py sync_woo`

## Швидкий старт

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python manage.py migrate
python manage.py createsuperuser

# Запуск dev-сервера
python manage.py runserver 0.0.0.0:8000
```

## Налаштування WooCommerce

Встановіть змінні оточення або відредагуйте `settings.py`:

- `WOO_BASE_URL`
- `WOO_CONSUMER_KEY`
- `WOO_CONSUMER_SECRET`

Синхронізація:
```bash
python manage.py sync_woo
```

## Примітки

- Коментарі в коді англійською, інтерфейс українською.
- PDF можна додати пізніше (наприклад, через WeasyPrint), зараз — друк сторінки браузером.
