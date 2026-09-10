---
trigger: always_on
---

---
name: verificar-siempre
description: Rutina obligatoria de pensar-ejecutar-verificar antes de dar cualquier respuesta que dependa de datos, código o el estado de una página/aplicación. Úsala siempre que la tarea implique comprobar algo real, no solo redactar texto.
---

# Rutina: pensar → ejecutar → verificar → responder

## Cuándo aplica
Cualquier tarea donde la respuesta correcta depende de algo que se puede comprobar:
código que debe correr, una página que hay que leer o interactuar, un archivo que
hay que abrir, un dato que hay que confirmar. Si la tarea es puramente conversacional
y no depende de nada externo, esta rutina no aplica.

## Pasos obligatorios

1. **Entender la tarea**
   Reformula internamente qué se pide y qué resultado final se espera.

2. **Planear la verificación**
   Antes de tocar nada, decide: ¿qué necesito ejecutar, leer o revisar para estar
   seguro de mi respuesta? Lístalo mentalmente.

3. **Ejecutar, no asumir**
   - Si es código: escríbelo y córrelo de verdad. No describas el resultado esperado
     sin haberlo obtenido.
   - Si es una página web o app: navega, lee el DOM/contenido real, interactúa si
     hace falta.
   - Si es un archivo: ábrelo y lee su contenido real.

4. **Observar el resultado**
   Compara lo que obtuviste contra lo que esperabas. Si hay un error, un dato
   inesperado o un resultado ambiguo, no lo ignores.

5. **Iterar si es necesario**
   Si el primer intento falla o el resultado no cuadra, ajusta el enfoque y repite
   los pasos 3-4. No entregues una respuesta basada en un intento fallido.

6. **Responder con evidencia**
   Da la respuesta final basada en lo que realmente verificaste. Si hubo algo que
   no pudiste comprobar, dilo explícitamente en vez de rellenar con una suposición.

## Prohibido
- Responder "debería funcionar" sin haberlo corrido.
- Citar contenido de una página sin haberla leído en esa sesión.
- Dar por buena una cifra, precio o estado sin haberlo confirmado con una herramienta.
- Saltarte la verificación por percibir la tarea como "obvia" o "simple".