# PATIA Restaurant: preparacion comercial

PATIA Restaurant se presenta a **$360 MXN al mes**. El codigo no contiene
un identificador de precio real: Render debe recibirlo mediante la variable
`STRIPE_RESTAURANT_PRICE_ID`.

Antes de habilitar contrataciones:

1. Crear en Stripe, en el mismo modo que los demas planes, un precio recurrente
   mensual de $360 MXN para PATIA Restaurant.
2. Guardar su identificador en `STRIPE_RESTAURANT_PRICE_ID` en Render.
3. Comprobar en modo de prueba que Checkout muestre PATIA Restaurant, $360 MXN
   al mes y metadata `plan_code=RESTAURANT`.
4. Comprobar el webhook antes de repetir el proceso en modo Live.

Si la variable no existe, el plan se muestra pero no inicia un Checkout falso:
la interfaz informa que la contratacion estara disponible proximamente.
