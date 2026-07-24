# Reporte mensual de PATIA Pro en Render

El envío está separado del servicio web. No debe ejecutarse desde una petición
HTTP ni desde el arranque de Gunicorn.

## Cron Job

Crear en Render un **Cron Job** conectado al mismo repositorio y base de datos:

- Runtime: Python 3.12
- Build command:
  `python -m pip install --upgrade pip && python -m pip install -r requirements.txt`
- Command:
  `python -m flask --app run.py send-monthly-reports`
- Schedule recomendado: `0 14 2 * *`

La programación equivale al día 2 de cada mes a las 14:00 UTC y envía el mes
anterior. El job usa la zona horaria configurada de cada negocio para calcular
los periodos.

El Cron Job necesita las mismas variables que el servicio web:

- `DATABASE_URL`
- `SECRET_KEY`
- `PUBLIC_BASE_URL`
- `RESEND_API_KEY`
- `RESEND_FROM`
- `STRIPE_SECRET_KEY`
- `STRIPE_STARTER_PRICE_ID` (o el alias temporal `STRIPE_PRICE_ID`)
- `STRIPE_PRO_PRICE_ID`
- `STRIPE_WEBHOOK_SECRET`
- `STRIPE_PAST_DUE_GRACE_DAYS`

No se deben copiar valores reales al repositorio.

## Prueba sin envío masivo

Para generar un periodo de una sola organización sin enviar correo:

```text
python -m flask --app run.py preview-monthly-report \
  --organization-id 123 --year 2026 --month 7
```

El comando valida que la organización tenga acceso Pro, registra la generación
idempotente y muestra solo metadatos seguros. No imprime el contenido completo
del correo ni credenciales.

## Idempotencia

La tabla `monthly_owner_report` tiene una restricción única por organización,
año y mes. Un periodo marcado como `sent` no vuelve a enviarse. Los fallos
quedan registrados para permitir un reintento controlado.
