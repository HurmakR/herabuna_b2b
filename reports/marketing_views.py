# reports/marketing_views.py
import socket
from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.core.mail import EmailMultiAlternatives
from django.http import JsonResponse
from django.shortcuts import render, redirect

from b2b.models import Brand, Product


def _is_superuser(u):
    return u.is_active and u.is_superuser


def validate_email_domain(request):
    """AJAX: lightweight domain existence check via DNS."""
    email = (request.GET.get("email") or "").strip().lower()
    if "@" not in email:
        return JsonResponse({"valid": False, "reason": "Невірний формат"})
    domain = email.split("@")[-1]
    try:
        socket.getaddrinfo(domain, None)
        return JsonResponse({"valid": True})
    except socket.gaierror:
        return JsonResponse({"valid": False, "reason": f"Домен {domain} не існує"})


def _build_email_html(products, show_price: bool) -> str:
    """Build HTML email based on the Herabuna commercial offer template."""

    def product_rows():
        rows = []
        for p in products:
            in_stock = p.stock_qty > 0
            stock_badge = (
                '<span style="display:inline-block;padding:2px 8px;background:#dcfce7;'
                'color:#166534;border-radius:4px;font-size:12px;font-family:Arial,sans-serif">В наявності</span>'
                if in_stock else
                '<span style="display:inline-block;padding:2px 8px;background:#f1f5f9;'
                'color:#64748b;border-radius:4px;font-size:12px;font-family:Arial,sans-serif">Очікується</span>'
            )
            price_cell = (
                f'<td align="right" style="padding:10px 12px;font-family:Arial,sans-serif;'
                f'font-size:14px;font-weight:bold;color:#0f172a;white-space:nowrap">'
                f'{p.wholesale_price}&nbsp;₴</td>'
                if show_price else ''
            )
            img_cell = ''
            if getattr(p, 'main_image_url', None):
                img_cell = (
                    f'<td style="padding:8px 12px;width:52px">'
                    f'<img src="{p.main_image_url}" width="44" height="44" '
                    f'style="border-radius:4px;object-fit:contain;border:1px solid #e2e8f0">'
                    f'</td>'
                )
            else:
                img_cell = '<td style="padding:8px 12px;width:52px"></td>'

            rows.append(f'''<tr style="border-bottom:1px solid #f1f5f9">
              {img_cell}
              <td style="padding:10px 8px">
                <div style="font-family:Arial,sans-serif;font-size:14px;color:#0f172a;font-weight:500">{p.name}</div>
                <div style="font-family:Arial,sans-serif;font-size:12px;color:#64748b;margin-top:2px">SKU: {p.sku}</div>
              </td>
              <td style="padding:10px 12px">{stock_badge}</td>
              {price_cell}
            </tr>''')
        return "\n".join(rows)

    price_header = (
        '<th align="right" style="padding:8px 12px;font-family:Arial,sans-serif;'
        'font-size:12px;color:#64748b;font-weight:normal;text-transform:uppercase;'
        'letter-spacing:.04em">Ціна (опт)</th>'
        if show_price else ''
    )

    rows_html = product_rows()

    return f'''<!DOCTYPE html>
<html lang="uk">
<head>
  <meta http-equiv="Content-Type" content="text/html; charset=UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Herabuna B2B — Комерційна пропозиція</title>
  <meta name="x-preheader" content="Прикормки OldGhost, HuaShi, Loonva — найкращі гуртові ціни. Необмежені по вазі поставки. Формуємо замовлення.">
  <style>
    body{{margin:0!important;padding:0!important;background:#f6f7fb}}
    table{{border-collapse:collapse!important}}
    img{{border:0;line-height:100%;outline:none;text-decoration:none;display:block}}
    @media only screen and (max-width:600px){{
      .container{{width:100%!important;min-width:100%!important}}
      .px{{padding-left:16px!important;padding-right:16px!important}}
      .cta{{display:block!important;width:100%!important;margin-bottom:8px!important}}
    }}
  </style>
</head>
<body style="margin:0;background:#f6f7fb">
<table role="presentation" width="100%" bgcolor="#f6f7fb">
<tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="600" class="container"
       style="width:600px;max-width:600px;background:#ffffff;border-radius:10px;overflow:hidden;border:1px solid #e9ecf2">

  <!-- Hero -->
  <tr>
    <td class="px" style="padding:24px 28px 8px 28px">
      <h1 style="margin:0;color:#0f172a;font-family:Arial,Helvetica,sans-serif;font-size:22px;line-height:1.3">
        Комерційна пропозиція для гуртових партнерів
      </h1>
      <p style="margin:12px 0 0 0;color:#334155;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.6">
        Постачаємо прикормки брендів <strong>OldGhost</strong>, <strong>HuaShi</strong>, <strong>Loonva</strong> та інші.
        Працюємо <strong>напряму з менеджерами брендів у Китаї</strong> — пропонуємо <strong>найкращі гуртові ціни</strong>,
        стабільні поставки та автентичність продукції.
      </p>
    </td>
  </tr>

  <!-- Advantages -->
  <tr>
    <td class="px" style="padding:8px 28px">
      <table role="presentation" width="100%" style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px">
        <tr><td style="padding:16px 18px">
          <h2 style="margin:0 0 8px 0;color:#004AAD;font-family:Arial,Helvetica,sans-serif;font-size:16px">Наші переваги</h2>
          <ul style="margin:8px 0 0 18px;padding:0;color:#334155;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.6">
            <li>Найкращі гуртові ціни завдяки прямим домовленостям із виробниками.</li>
            <li><strong>Каталоги у PDF</strong> по кожному бренду — на запит.</li>
            <li>Стабільна логістика: море (основні партії) + авіа (швидке поповнення).</li>
            <li>Гнучка система знижок для постійних партнерів.</li>
            <li>Зручний B2B-портал для замовлень і відстеження відправлень.</li>
          </ul>
        </td></tr>
      </table>
    </td>
  </tr>

  <!-- Price list -->
  <tr>
    <td class="px" style="padding:16px 28px 8px 28px">
      <h2 style="margin:0 0 12px 0;color:#0f172a;font-family:Arial,Helvetica,sans-serif;font-size:16px">
        Актуальний прайс
      </h2>
      <table role="presentation" width="100%"
             style="border:1px solid #e2e8f0;border-radius:8px;overflow:hidden">
        <thead>
          <tr style="background:#f8fafc">
            <th style="width:52px"></th>
            <th align="left" style="padding:8px 8px;font-family:Arial,sans-serif;font-size:12px;
                color:#64748b;font-weight:normal;text-transform:uppercase;letter-spacing:.04em">Товар</th>
            <th style="padding:8px 12px;font-family:Arial,sans-serif;font-size:12px;
                color:#64748b;font-weight:normal;text-transform:uppercase;letter-spacing:.04em">Наявність</th>
            {price_header}
          </tr>
        </thead>
        <tbody>
          {rows_html}
        </tbody>
      </table>
    </td>
  </tr>

  <!-- CTA -->
  <tr>
    <td class="px" align="left" style="padding:12px 28px 8px 28px">
      <table role="presentation"><tr>
        <td class="cta" align="center" bgcolor="#0ea5e9" style="border-radius:6px">
          <a href="https://b2b.herabuna.com.ua/" target="_blank"
             style="display:inline-block;padding:12px 18px;font-family:Arial,Helvetica,sans-serif;
                    font-size:14px;color:#ffffff;text-decoration:none;border-radius:6px">
            Перейти на сайт
          </a>
        </td>
      </tr></table>
    </td>
  </tr>

  <!-- Divider -->
  <tr><td style="height:8px"></td></tr>
  <tr><td style="padding:0 28px"><hr style="height:1px;border:0;background:#e5e7eb"></td></tr>

  <!-- Contacts -->
  <tr>
    <td class="px" style="padding:10px 28px 24px 28px">
      <p style="margin:0 0 6px 0;color:#0f172a;font-family:Arial,Helvetica,sans-serif;font-size:14px">
        <strong>Контакти</strong>
      </p>
      <p style="margin:0;color:#334155;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.6">
        🌐 <a href="https://herabuna.com.ua/hurt/" style="color:#004AAD;text-decoration:none">herabuna.com.ua</a><br>
        ✉️ <a href="mailto:mail@herabuna.com.ua" style="color:#004AAD;text-decoration:none">mail@herabuna.com.ua</a><br>
        📱 +380&nbsp;63&nbsp;974&nbsp;6086
      </p>
    </td>
  </tr>

  <!-- Footer -->
  <tr>
    <td align="center" style="background:#0b1220;padding:14px 20px">
      <p style="margin:0;color:#94a3b8;font-family:Arial,Helvetica,sans-serif;font-size:12px">
        © Herabuna B2B · Всі права захищено
      </p>
    </td>
  </tr>

</table>
</td></tr>
</table>
</body>
</html>'''


@user_passes_test(_is_superuser)
def marketing_mailing(request):
    brands = Brand.objects.order_by("name")

    if request.method == "POST":
        recipients = [e.strip().lower() for e in request.POST.getlist("emails") if e.strip()]
        recipients = list(dict.fromkeys(recipients))

        if not recipients:
            messages.error(request, "Вкажіть хоча б одну email-адресу.")
            return redirect("reports:marketing_mailing")

        brand_ids  = request.POST.getlist("brands")
        show_price = bool(request.POST.get("show_price"))

        stock_filter = request.POST.get("stock_filter", "in_stock")

        qs = (
            Product.objects
            .filter(is_active=True)
            .select_related("brand")
            .order_by("brand__name", "name")
        )
        if stock_filter == "in_stock":
            qs = qs.filter(stock_qty__gt=0)
        elif stock_filter == "out_stock":
            qs = qs.filter(stock_qty=0)
        # "all" — без фільтра

        if brand_ids:
            qs = qs.filter(brand_id__in=brand_ids)

        products = list(qs)
        if not products:
            messages.warning(request, "Немає товарів за вибраними фільтрами.")
            return redirect("reports:marketing_mailing")

        html_body = _build_email_html(products, show_price)
        subject   = "Комерційна пропозиція — Herabuna B2B"

        sent, failed = 0, []
        for email in recipients:
            try:
                msg = EmailMultiAlternatives(
                    subject=subject,
                    body="Будь ласка, перегляньте цей лист у HTML-клієнті.",
                    from_email=None,
                    to=[email],
                )
                msg.attach_alternative(html_body, "text/html")
                msg.send()
                sent += 1
            except Exception as e:
                failed.append(f"{email}: {e}")

        if sent:
            messages.success(request, f"✓ Відправлено: {sent} листів.")
        for err in failed:
            messages.warning(request, f"Помилка: {err}")

        return redirect("reports:marketing_mailing")

    return render(request, "reports/marketing_mailing.html", {"brands": brands})
