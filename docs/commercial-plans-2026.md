# PATIA: planes comerciales

| Nombre | Código interno conservado | Mensual MXN | Personas |
| --- | --- | --- | --- |
| Esencial | STARTER | 199 | 2 |
| Control | PRO | 349 | 5 |
| Cocina | RESTAURANT | 499 | 5 |

Cocina incluye todas las capacidades de Control, soporte prioritario y recetas.
Los códigos internos se conservan para no alterar usuarios ni suscripciones.
El tipo de negocio queda fijo después del registro: Configuración permite
editar los demás datos, pero rechaza cualquier cambio de tipo incluso por POST.
Las cuentas registradas como tienda tampoco pueden contratar ni cambiar a
Cocina desde Suscripción; la restricción se aplica en la pantalla y en el servidor.

## Configuración de cobros antes de publicar

- Mantener STRIPE_STARTER_PRICE_ID (199 MXN/mes) y STRIPE_PRO_PRICE_ID
  (349 MXN/mes). Los nombres visibles los define PATIA.
- Crear un precio mensual nuevo de 499 MXN para Cocina y guardar su ID
  en STRIPE_COCINA_PRICE_ID en Render. Probar primero con un precio de pruebas.
- Conservar STRIPE_RESTAURANT_PRICE_ID con el precio anterior: se usa para
  reconocer suscripciones existentes y sus webhooks, nunca para vender Cocina.
- No editar ni migrar las suscripciones existentes. La pantalla de suscripción
  muestra el importe obtenido de Stripe; si no está disponible, remite a
  Administrar pagos en lugar de mostrar el precio de nueva contratación.
- Sin STRIPE_COCINA_PRICE_ID, Cocina aparece como próximamente y no permite
  checkout. No se presenta el antiguo precio como si correspondiera a 499 MXN.

## Cancelación

Negocio > Suscripción muestra Cancelar suscripción para una suscripción
administrable activa. Programa cancel_at_period_end=True, conserva el acceso
pagado y muestra Reactivar suscripción mientras termina el periodo.
Los formularios conservan CSRF y permisos del propietario.

## Verificación aislada

Las pruebas usan SQLite temporal y mocks de Stripe. No ejecutan cobros reales,
cancelaciones reales ni escrituras en producción.
