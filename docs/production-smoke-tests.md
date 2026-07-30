# Smoke tests de producción

Los smoke tests públicos pueden ejecutarse sin credenciales. Las rutas
autenticadas requieren exclusivamente una cuenta QA independiente.

Nunca uses:

- la cuenta personal del fundador o de otro integrante;
- una cuenta administrativa;
- una cuenta activa de un cliente;
- credenciales recordadas de una validación anterior.

Si no existen credenciales QA, el script omite las rutas autenticadas y lo
reporta explícitamente. No existe un fallback a cuentas conocidas.

## Configuración

Configura las variables fuera de Git:

```powershell
$env:PATIA_QA_BASE_URL = "https://patiaapp.com"
$env:PATIA_QA_EMAIL = "qa-smoke@dominio-seguro.example"
$env:PATIA_QA_PASSWORD = "<contraseña QA>"
$env:PATIA_QA_ACCOUNT_CONFIRMED = "true"

# Si existe otro correo personal o administrativo, protégelo también:
$env:PATIA_ADMIN_EMAIL = "administrador@dominio.example"
$env:PATIA_SMOKE_FORBIDDEN_EMAILS = "personal@dominio.example,otra@dominio.example"

python scripts/smoke_production.py
```

`PATIA_QA_EMAIL` debe ser distinto del administrador actual y de todos los
correos incluidos en `PATIA_SMOKE_FORBIDDEN_EMAILS`. La contraseña nunca se
imprime.

## Sin cuenta QA

Ejecuta:

```powershell
Remove-Item Env:PATIA_QA_EMAIL -ErrorAction SilentlyContinue
Remove-Item Env:PATIA_QA_PASSWORD -ErrorAction SilentlyContinue
python scripts/smoke_production.py
```

El resultado debe incluir:

```text
SKIP autenticado: no existen credenciales PATIA_QA_* explícitas.
```

La ausencia de credenciales QA nunca autoriza reutilizar una cuenta personal.
