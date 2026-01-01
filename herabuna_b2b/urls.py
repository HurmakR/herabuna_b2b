from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include(("b2b.urls", "b2b"), namespace="b2b")),
    path("reports/", include(("reports.urls", "reports"), namespace="reports")),
    path("warehouse/", include(("warehouse.urls", "warehouse"), namespace="warehouse")),
]
