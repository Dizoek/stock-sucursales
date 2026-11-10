# Bsale → Shopify: stock por sucursal

Cada 10 minutos lee en Bsale el stock disponible de las sucursales 1 (Casa Matriz),
2 (Portal Lyon) y 5 (Barrio Italia) y lo guarda en cada variante de Shopify, en el
metafield `vchile.stock_sucursales`, por ejemplo `{"1":752,"2":20,"5":14}`.
El bloque «Stock en sucursales» de la ficha de producto lo muestra.

No toca el inventario de Shopify (ese lo sincroniza Pivot). Solo escribe las
variantes cuyo stock cambió.

## Cómo corre
GitHub Actions ejecuta `sync.py` según `.github/workflows/sync.yml`.
Las credenciales están en Settings → Secrets and variables → Actions:

| Secret | Qué es |
|---|---|
| `BSALE_TOKEN` | Token de API de Bsale (producción) |
| `SHOPIFY_CLIENT_ID` | Client ID de la app del Dev Dashboard de Shopify |
| `SHOPIFY_CLIENT_SECRET` | Client secret de la misma app |

La app de Shopify necesita los permisos `read_products` y `write_products`.

## Probar a mano
Pestaña Actions → «Stock por sucursal» → Run workflow:
- **prueba (no escribe):** muestra qué cambiaría para un SKU.
- **un SKU (escribe):** actualiza solo ese SKU.
- **completo (escribe):** lo mismo que hace el horario.

## Pausar
Actions → «Stock por sucursal» → menú «…» → Disable workflow.
