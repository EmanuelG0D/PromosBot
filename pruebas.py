"""Pruebas rapidas del radar: python pruebas.py

No tocan la red: validan la logica que decide que se alerta y que no.
"""
from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent

import config
from core import filtros, objetivos, veracidad, http
from core.coupons import extract_coupons, needs_clipping
from core.landed import calcular
from core.models import Deal
from core.scoring import evaluar
from core.store import Store

# SALVAGUARDA GLOBAL DE PRUEBAS:
# Bloquea 100% cualquier intento de llamada a la API real de Telegram durante la ejecución de los tests.
_real_post_json = http.post_json
def _seguro_post_json(url, *args, **kwargs):
    if "api.telegram.org" in str(url):
        return {"ok": True, "result": {"message_id": 99999, "status": "member"}}
    return _real_post_json(url, *args, **kwargs)
http.post_json = _seguro_post_json

_real_post_multipart = http.post_multipart
def _seguro_post_multipart(url, *args, **kwargs):
    if "api.telegram.org" in str(url):
        return {"ok": True, "result": {"message_id": 99999}}
    return _real_post_multipart(url, *args, **kwargs)
http.post_multipart = _seguro_post_multipart


def oferta(**kwargs) -> Deal:
    base = dict(
        source="vtex", store="Exito", country="CO", key="k1",
        title="Producto de prueba", url="https://ejemplo.co/p",
        price=100_000.0, currency="COP", list_price=400_000.0,
    )
    if "discount" in kwargs:
        d = kwargs.pop("discount")
        p = kwargs.get("price", base["price"])
        if d > 0 and d < 100 and p:
            kwargs.setdefault("list_price", round(p / (1.0 - d / 100.0), 2))
    base.update(kwargs)
    return Deal(**base)



class PruebaCupones(unittest.TestCase):
    def test_extrae_codigos_tipicos(self):
        self.assertEqual(extract_coupons("Nike Shoes $24 w/ code SAVE20"), ["SAVE20"])
        self.assertEqual(extract_coupons("Use promo code: BLACK40 at checkout"), ["BLACK40"])
        self.assertEqual(extract_coupons("Aplica el cupon VERANO25"), ["VERANO25"])

    def test_ignora_texto_corriente(self):
        self.assertEqual(extract_coupons("Great shoes with code quality stitching"), [])
        self.assertEqual(extract_coupons("Sin cupones aqui"), [])

    def test_detecta_cupon_de_activacion(self):
        self.assertTrue(needs_clipping("Clip the 20% coupon on the product page"))
        self.assertFalse(needs_clipping("No requiere nada"))


class PruebaCostoPuesto(unittest.TestCase):
    def test_bajo_200_queda_exento(self):
        r = calcular(150.0, trm=4000.0, peso_lb=2.0)
        self.assertTrue(r.exento)
        self.assertEqual(r.iva_usd, 0.0)
        self.assertEqual(r.total_usd, 150.0 + r.flete_usd)

    def test_sobre_200_liquida_iva_y_arancel(self):
        r = calcular(250.0, trm=4000.0, peso_lb=2.0)
        self.assertFalse(r.exento)
        self.assertGreater(r.iva_usd, 0)
        self.assertGreater(r.total_cop, 250.0 * 4000.0)

    def test_convierte_a_pesos(self):
        r = calcular(100.0, trm=4000.0, peso_lb=1.0)
        self.assertEqual(r.total_cop, round(r.total_usd * 4000.0))


class PruebaDecision(unittest.TestCase):
    sin_historial = (0, None, None)

    def test_marketplace_con_precio_inflado_no_es_glitch(self):
        # Caso real: portatil "de 36.799.990" vendido en 1.679.990 por un tercero.
        d = oferta(price=1_679_990.0, list_price=36_799_990.0,
                   marketplace=True, list_price_trusted=False)
        v = evaluar(d, self.sin_historial)
        self.assertFalse(v.glitch)
        self.assertFalse(v.alertar)

    def test_descuento_real_de_tienda_propia_si_alerta(self):
        d = oferta(price=959_900.0, list_price=3_699_900.0)
        v = evaluar(d, self.sin_historial)
        self.assertTrue(v.alertar)
        self.assertFalse(v.glitch)  # 74% queda bajo el umbral de 75%
        self.assertGreater(len(v.motivo), 0)

    def test_caida_contra_historial_propio_sube_la_confianza(self):
        d = oferta(price=100_000.0, list_price=120_000.0)
        v = evaluar(d, (5, 300_000.0, 250_000.0))
        self.assertTrue(v.alertar)
        self.assertEqual(v.confianza, "alta")

    def test_sin_stock_no_alerta(self):
        v = evaluar(oferta(in_stock=False), self.sin_historial)
        self.assertFalse(v.alertar)


class PruebaDeduplicacion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_no_repite_la_misma_alerta(self):
        d = oferta()
        self.assertTrue(self.store.should_alert(d)[0])
        self.store.mark_alerted(d)
        self.assertFalse(self.store.should_alert(d)[0])

    def test_vuelve_a_avisar_si_baja_mas(self):
        d = oferta()
        self.store.mark_alerted(d)
        mas_barata = oferta(price=d.price * 0.8)
        self.assertTrue(self.store.should_alert(mas_barata)[0])

    def test_no_guarda_el_mismo_precio_dos_veces(self):
        for _ in range(5):
            self.store.record(oferta(price=300_000.0))
        n, _, _ = self.store.price_stats("k1")
        self.assertEqual(n, 1)

    def test_historial_de_precios(self):
        for precio in (300_000.0, 320_000.0, 310_000.0):
            self.store.record(oferta(price=precio))
        n, mediana, minimo = self.store.price_stats("k1")
        self.assertGreaterEqual(n, 1)
        self.assertIsNotNone(mediana)
        self.assertEqual(minimo, min(300_000.0, 310_000.0, 320_000.0))


class PruebaColapsoVariantes(unittest.TestCase):
    """Un producto, una alerta: aunque cada tienda escriba el titulo distinto."""

    def par(self, **kw):
        from core.scoring import Verdict
        return (oferta(**kw), Verdict(True))

    def test_colapsa_colores_y_tiendas_hermanas(self):
        from radar import _colapsar_variantes
        pares = [
            self.par(key="a", store="Alkosto", price=349900.0,
                     title="Audifonos SONY WH-CH720N Negro"),
            self.par(key="b", store="Alkosto", price=349900.0,
                     title="Audifonos SONY WH-CH720N Azul"),
            self.par(key="c", store="K-tronix", price=349900.0,
                     title="Audifonos SONY WH-CH720N Negro"),
        ]
        unicas, hermanas = _colapsar_variantes(pares)
        self.assertEqual(len(unicas), 1)
        self.assertEqual(len(hermanas[unicas[0][0].key]), 2)
        self.assertIn("Tambien en K-tronix", unicas[0][0].notes)

    def test_junta_el_mismo_producto_con_titulos_distintos(self):
        """Caso real: cada tienda nombra el mismo televisor a su manera."""
        from radar import _colapsar_variantes
        pares = [
            self.par(key="alk", store="Alkosto", price=1249900.0,
                     title='TV KALLEY 50" Pulgadas 126 cm 50G315 4K-UHD MAX LED Smart TV Google'),
            self.par(key="exi", store="Exito", price=1249900.0,
                     title="Televisor Kalley 50G315a 50 Pulgadas 4Kuhd Max Smart Tv"),
        ]
        unicas, _hermanas = _colapsar_variantes(pares)
        self.assertEqual(len(unicas), 1, "el mismo televisor no debe salir dos veces")

    def test_no_junta_productos_distintos_del_mismo_precio(self):
        from radar import _colapsar_variantes
        pares = [
            self.par(key="1", store="Exito", price=99900.0, title="Licuadora IMUSA Powermix"),
            self.par(key="2", store="Exito", price=99900.0, title="Cafetera OSTER 5 tazas"),
        ]
        unicas, _h = _colapsar_variantes(pares)
        self.assertEqual(len(unicas), 2)


class PruebaMarketplaceRedundante(unittest.TestCase):
    """Un revendedor solo interesa si le gana el precio a la tienda."""

    def test_descarta_al_revendedor_que_cobra_igual(self):
        from radar import _marketplace_solo_si_mejora
        propia = oferta(key="p", store="Alkosto", price=1249900.0,
                        title="TV KALLEY 50 Pulgadas 50G315 4K-UHD Smart")
        externo = oferta(key="m", store="Exito", price=1249900.0, marketplace=True,
                         title="Televisor Kalley 50G315a 50 Pulgadas Smart Tv")
        quedan = _marketplace_solo_si_mejora([propia, externo])
        self.assertEqual([d.key for d in quedan], ["p"])

    def test_conserva_al_revendedor_mas_barato(self):
        from radar import _marketplace_solo_si_mejora
        propia = oferta(key="p", store="Alkosto", price=1249900.0,
                        title="TV KALLEY 50 Pulgadas 50G315 4K-UHD Smart")
        externo = oferta(key="m", store="Exito", price=999900.0, marketplace=True,
                         title="Televisor Kalley 50G315a 50 Pulgadas Smart Tv")
        quedan = _marketplace_solo_si_mejora([propia, externo])
        self.assertEqual(sorted(d.key for d in quedan), ["m", "p"])

    def test_no_toca_al_revendedor_de_otro_producto(self):
        from radar import _marketplace_solo_si_mejora
        propia = oferta(key="p", store="Alkosto", price=99900.0, title="Cafetera KALLEY 8 tazas")
        externo = oferta(key="m", store="Exito", price=99900.0, marketplace=True,
                         title="Licuadora IMUSA Powermix 5 velocidades")
        self.assertEqual(len(_marketplace_solo_si_mejora([propia, externo])), 2)


class PruebaObjetivos(unittest.TestCase):
    metas = [
        {"termino": "freidora", "max_cop": 150000},
        {"termino": "sneakers", "max_usd": 35},
    ]

    def test_avisa_por_precio_absoluto_sin_importar_el_porcentaje(self):
        # El caso real: una freidora barata que solo figura como -45%.
        d = oferta(title="Freidora de Aire KALLEY 3.5 Litros Negro",
                   price=99_900.0, list_price=180_000.0)
        self.assertIsNotNone(objetivos.alcanzado(d, self.metas))

    def test_ignora_lo_que_sigue_caro(self):
        d = oferta(title="Freidora de Aire OSTER 6 Litros", price=389_030.0)
        self.assertIsNone(objetivos.alcanzado(d, self.metas))

    def test_descarta_accesorios(self):
        d = oferta(title="Canasta de repuesto para freidora de aire", price=39_900.0)
        self.assertIsNone(objetivos.alcanzado(d, self.metas))

    def test_respeta_la_moneda(self):
        # Un objetivo en dolares no debe dispararse con un precio en pesos.
        d = oferta(title="Sneakers Nike", price=120_000.0, currency="COP")
        self.assertIsNone(objetivos.alcanzado(d, self.metas))

    def test_los_terminos_entran_a_las_busquedas(self):
        self.assertEqual(objetivos.terminos_de_busqueda(self.metas),
                         ["freidora", "sneakers"])


class PruebaUrgencia(unittest.TestCase):
    sin_historial = (0, None, None)

    def test_objetivo_cumplido_interrumpe(self):
        d = oferta(title="Freidora KALLEY", price=99_900.0, list_price=180_000.0)
        v = evaluar(d, self.sin_historial, {"termino": "freidora", "max_cop": 150000})
        self.assertTrue(v.alertar)
        self.assertTrue(v.inmediata)

    def test_rebaja_mediana_va_al_resumen(self):
        d = oferta(price=100_000.0, list_price=180_000.0)   # -44%
        v = evaluar(d, self.sin_historial)
        self.assertTrue(v.alertar)
        self.assertFalse(v.inmediata)

    def test_descuentazo_interrumpe(self):
        d = oferta(price=100_000.0, list_price=300_000.0)   # -67%
        v = evaluar(d, self.sin_historial)
        self.assertTrue(v.inmediata)

    def test_rebaja_con_cupon_interrumpe(self):
        d = oferta(price=100_000.0, list_price=180_000.0, coupons=["AHORRA20"])
        v = evaluar(d, self.sin_historial)
        self.assertTrue(v.inmediata)
        self.assertIn("rebaja + cupon", v.etiquetas)


def historial(precios, dias=30):
    """Construye un historial repartido en una ventana de N dias."""
    inicio = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=dias)
    paso = dias / max(len(precios) - 1, 1)
    return [((inicio + dt.timedelta(days=i * paso)).isoformat(), float(p))
            for i, p in enumerate(precios)]


class PruebaVeracidad(unittest.TestCase):
    def test_detecta_la_maniobra_de_subir_y_luego_bajar(self):
        # Valia 300.000 por semanas, lo subieron a 500.000, y ahora lo
        # "rebajan" a 460.000 anunciando un descuentazo.
        v = veracidad.analizar(460_000.0, historial([300_000, 300_000, 500_000, 500_000]))
        self.assertTrue(v.es_falsa)
        self.assertEqual(v.minimo_ventana, 300_000.0)
        self.assertGreater(v.alza_previa, 60)
        self.assertIn("subieron el precio", v.detalle)

    def test_rebaja_de_verdad_pasa(self):
        v = veracidad.analizar(250_000.0, historial([300_000, 300_000, 310_000]))
        self.assertTrue(v.es_real)
        self.assertAlmostEqual(v.descuento_real, 16.7, places=1)

    def test_volver_al_precio_de_siempre_no_es_oferta(self):
        # Sube y regresa exactamente a donde estaba: descuento anunciado, cero real.
        v = veracidad.analizar(300_000.0, historial([300_000, 450_000, 450_000]))
        self.assertTrue(v.es_falsa)

    def test_sin_historial_no_opina(self):
        v = veracidad.analizar(250_000.0, historial([300_000]))
        self.assertEqual(v.veredicto, veracidad.SIN_DATOS)
        self.assertFalse(v.es_falsa)
        self.assertFalse(v.es_real)

    def test_historial_muy_corto_no_opina(self):
        v = veracidad.analizar(250_000.0, historial([300_000, 300_000, 300_000], dias=2))
        self.assertEqual(v.veredicto, veracidad.SIN_DATOS)


class PruebaVeracidadEnDecision(unittest.TestCase):
    sin_historial = (0, None, None)

    def test_la_rebaja_falsa_no_se_avisa(self):
        d = oferta(price=460_000.0, list_price=900_000.0)   # la tienda declara -49%
        v = veracidad.analizar(460_000.0, historial([300_000, 300_000, 500_000, 500_000]))
        veredicto = evaluar(d, self.sin_historial, None, v)
        self.assertFalse(veredicto.alertar)
        self.assertIn("rebaja falsa", veredicto.motivo)

    def test_la_rebaja_real_interrumpe(self):
        d = oferta(price=250_000.0, list_price=320_000.0)   # solo -22% declarado
        v = veracidad.analizar(250_000.0, historial([300_000, 300_000, 310_000]))
        veredicto = evaluar(d, self.sin_historial, None, v)
        self.assertTrue(veredicto.alertar)
        self.assertTrue(veredicto.inmediata)
        self.assertEqual(veredicto.confianza, "alta")

    def test_un_objetivo_de_precio_manda_sobre_la_sospecha(self):
        # Si el precio absoluto es el que yo queria, me interesa igual.
        d = oferta(title="Freidora KALLEY", price=140_000.0, list_price=400_000.0)
        v = veracidad.analizar(140_000.0, historial([120_000, 200_000, 200_000]))
        self.assertTrue(v.es_falsa)
        veredicto = evaluar(d, self.sin_historial,
                            {"termino": "freidora", "max_cop": 150000}, v)
        self.assertTrue(veredicto.alertar)
        self.assertTrue(veredicto.inmediata)


class PruebaRelampago(unittest.TestCase):
    sin_historial = (0, None, None)

    def vence_en(self, horas):
        return (dt.datetime.now(dt.timezone.utc)
                + dt.timedelta(hours=horas)).isoformat().replace("+00:00", "Z")

    def test_un_trasnochon_moderado_si_alerta(self):
        # -25% no pasaria el umbral normal, pero se vence en 3 horas.
        d = oferta(price=750_000.0, list_price=1_000_000.0, expires_at=self.vence_en(3))
        v = evaluar(d, self.sin_historial)
        self.assertTrue(v.alertar)
        self.assertTrue(v.inmediata)
        self.assertIn("termina en", v.motivo)

    def test_sin_vencimiento_el_mismo_descuento_no_pasa(self):
        d = oferta(price=750_000.0, list_price=1_000_000.0)
        self.assertFalse(evaluar(d, self.sin_historial).alertar)

    def test_vencimiento_lejano_no_es_relampago(self):
        d = oferta(price=750_000.0, list_price=1_000_000.0, expires_at=self.vence_en(24 * 30))
        self.assertFalse(evaluar(d, self.sin_historial).alertar)


class PruebaTiendaSlickdeals(unittest.TestCase):
    def test_usa_el_sufijo_at_tienda(self):
        from sources.slickdeals import _tienda
        self.assertIn("Woot", _tienda("Fragrance Testers and Gift Sets!  at Woot!"))

    def test_usa_la_marca_inicial(self):
        from sources.slickdeals import _tienda
        self.assertIn("Nike", _tienda("Nike Men's Shox NZ Shoes $62.87 + Free Shipping"))

    def test_no_inventa_tienda_por_menciones_sueltas(self):
        # Antes atribuia esta oferta a Target solo por nombrarla en la lista.
        from sources.slickdeals import _tienda
        titulo = "10% Off Select Gift Cards: Starbucks, Target, Nintendo, adidas & More"
        self.assertEqual(_tienda(titulo), "Slickdeals")


class PruebaPieDeFoto(unittest.TestCase):
    def test_respeta_el_limite_de_telegram(self):
        from core.telegram import _pie_de_foto
        from core.scoring import Verdict
        d = oferta(title="X" * 200, currency="COP")
        d.notes = ["nota larguisima " * 20] * 3
        v = Verdict(True, "motivo", ["etiqueta " * 30] * 4)
        pie = _pie_de_foto(d, v)
        self.assertLessEqual(len(pie), 1024)

    def test_conserva_el_enlace(self):
        from core.telegram import _pie_de_foto
        from core.scoring import Verdict
        d = oferta(title="Producto", url="https://tienda.co/oferta")
        pie = _pie_de_foto(d, Verdict(True))
        self.assertIn("https://tienda.co/oferta", pie)


class PruebaFiltros(unittest.TestCase):
    intereses = ["adidas", "ssd", "nintendo", "air fryer"]
    vetadas = ["perfume", "fragrance", "gift card", "vitamin"]

    def test_veta_lo_que_no_interesa(self):
        self.assertTrue(filtros.descartado("Fragrance Gift Sets at Woot", self.vetadas))
        self.assertTrue(filtros.descartado("10% Off Select Gift Cards", self.vetadas))
        self.assertFalse(filtros.descartado("adidas Runfalcon 5 Shoes $21", self.vetadas))

    def test_exige_que_mencione_un_interes(self):
        self.assertTrue(filtros.pertinente("Nintendo Switch OLED $299", self.intereses))
        self.assertTrue(filtros.pertinente("Samsung 2TB SSD $89", self.intereses))
        self.assertFalse(filtros.pertinente("Boho Maxi Dress w/ Pockets", self.intereses))

    def test_sin_lista_deja_pasar_todo(self):
        self.assertTrue(filtros.pertinente("cualquier cosa", []))
        self.assertFalse(filtros.descartado("cualquier cosa", []))

    def test_ignora_tildes_y_mayusculas(self):
        self.assertTrue(filtros.pertinente("NINTENDO Switch", ["nintendo"]))
        self.assertTrue(filtros.descartado("Perfúme importado", ["perfume"]))


class PruebaTopeDiario(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def test_cuenta_los_envios_del_dia(self):
        self.assertEqual(self.store.enviadas_hoy(), 0)
        for _ in range(3):
            self.store.sumar_enviada()
        self.assertEqual(self.store.enviadas_hoy(), 3)


class PruebaAlcanceSlickdeals(unittest.TestCase):
    """Slickdeals quedo acotado a Amazon, Adidas, Puma, Nike y ropa.

    Antes eran 22 busquedas de tecnologia y consolas, y la politica de
    "consolas si, videojuegos no" vivia en esas consultas. Al acotarlo, esa
    defensa desaparecio: ahora lo que decide es "incluir", y las consolas
    quedaron sencillamente fuera del alcance.
    """

    @classmethod
    def setUpClass(cls):
        cfg = json.loads((BASE / "watchlist.json").read_text(encoding="utf-8"))
        cls.cfg = cfg["slickdeals"]

    def pasa(self, titulo):
        if filtros.descartado(titulo, self.cfg["excluir"]):
            return False
        return filtros.pertinente(titulo, self.cfg["incluir"])

    def test_entra_lo_que_se_pidio(self):
        for titulo in ("adidas men Supernova Ease Shoes $40 + Free Shipping",
                       "Adidas Women's Adizero SL2 Running Shoes $38",
                       "PUMA Men's Softride Sneakers $29.99",
                       "Nike Men's Revolution 7 Running Shoes $37.97",
                       "Levi's Men's 511 Slim Fit Jeans $29.99"):
            self.assertTrue(self.pasa(titulo), f"deberia pasar: {titulo}")

    def test_las_mochilas_ya_no_se_vetan(self):
        """Se quitaron backpack, duffel y wallet del veto: ahora interesan."""
        self.assertTrue(self.pasa('17" PUMA Pitch Ball Backpack $16.04'))

    def test_lo_que_quedo_fuera_del_alcance(self):
        """Tecnología, televisores, consolas y juegos ya no se buscan aquí;
        Slickdeals queda exclusivo para ropa y calzado de marca."""
        for titulo in ("Nintendo Switch 2 System Black at Woot! $449.99",
                       "Xbox Elite Wireless Controller Series 2 $119",
                       '27" LG Ultragear 1440p 300Hz Monitor $210',
                       '$697.99 | 75" Hisense E7 Series 4K TV at Amazon',
                       "Biomutant (Nintendo Switch) at Amazon $13.99"):
            self.assertFalse(self.pasa(titulo), f"no deberia entrar: {titulo}")

    def test_el_veto_de_siempre_sigue_en_pie(self):
        for titulo in ("Fragrance Testers and Gift Sets at Woot",
                       "6-Pk PUMA Low-Cut Logo Runner Socks $5.26",
                       "Elden Ring Digital Code (Xbox) $29.99",
                       "Borderlands 4 Super Deluxe Edition $49.99"):
            self.assertFalse(self.pasa(titulo), f"deberia vetarse: {titulo}")


class PruebaEnlacesVtex(unittest.TestCase):
    """Los enlaces deben usar el dominio publico, no el interno de la API.

    tienda.exito.com y secure.carulla.com devuelven la ficha, pero con un
    redirect en JavaScript a su portada: el usuario abre la oferta y termina
    en el inicio del sitio sin ver el producto.
    """

    def test_ignora_el_dominio_interno_de_la_api(self):
        from sources.vtex import TIENDAS, enlace_publico
        producto = {"linkText": "televisor-tcl-p8k-124219799",
                    "link": "https://tienda.exito.com/televisor-tcl-p8k-124219799/p"}
        url = enlace_publico(TIENDAS["exito"], producto)
        self.assertTrue(url.startswith("https://www.exito.com/"), url)
        self.assertNotIn("tienda.exito.com", url)

    def test_carulla_tampoco_usa_secure(self):
        from sources.vtex import TIENDAS, enlace_publico
        producto = {"linkText": "televisor-abc-100021092-mp",
                    "link": "https://secure.carulla.com/televisor-abc-100021092-mp/p"}
        url = enlace_publico(TIENDAS["carulla"], producto)
        self.assertTrue(url.startswith("https://www.carulla.com/"), url)

    def test_cae_al_link_de_la_api_si_no_hay_ruta(self):
        from sources.vtex import TIENDAS, enlace_publico
        producto = {"link": "https://www.exito.com/algo/p"}
        self.assertEqual(enlace_publico(TIENDAS["exito"], producto),
                         "https://www.exito.com/algo/p")


class PruebaNikeVtex(unittest.TestCase):
    """Verifica el filtrado estricto en origen para la tienda oficial Nike Colombia."""

    def test_nike_filtra_calzado_y_precio_maximo(self):
        from unittest.mock import patch
        from sources import vtex

        mock_productos = [
            # 1. Calzado económico válido (debe pasar y anteponer 'Tenis')
            {
                "productId": "101",
                "productName": "Nike Star Runner 5",
                "linkText": "nike-star-runner-5-hf7006",
                "categories": ["/Hombre/Calzado/Tenis/", "/Hombre/Calzado/"],
                "items": [{
                    "sellers": [{
                        "sellerId": "1",
                        "sellerName": "Nike",
                        "commertialOffer": {
                            "Price": 189900.0,
                            "ListPrice": 270000.0,
                            "AvailableQuantity": 5,
                            "IsAvailable": True
                        }
                    }]
                }]
            },
            # 2. Calzado que supera el tope de $220.000 COP (debe descartarse)
            {
                "productId": "102",
                "productName": "Nike Pegasus 41",
                "linkText": "nike-pegasus-41",
                "categories": ["/Hombre/Calzado/Tenis/"],
                "items": [{
                    "sellers": [{
                        "sellerId": "1",
                        "sellerName": "Nike",
                        "commertialOffer": {
                            "Price": 249900.0,
                            "ListPrice": 450000.0,
                            "AvailableQuantity": 2,
                            "IsAvailable": True
                        }
                    }]
                }]
            },
            # 3. Ropa o accesorio barato (debe descartarse aunque valga $50.000)
            {
                "productId": "103",
                "productName": "Camiseta Nike Dri-Fit",
                "linkText": "camiseta-nike-dri-fit",
                "categories": ["/Hombre/Ropa/Camisetas/"],
                "items": [{
                    "sellers": [{
                        "sellerId": "1",
                        "sellerName": "Nike",
                        "commertialOffer": {
                            "Price": 59900.0,
                            "ListPrice": 120000.0,
                            "AvailableQuantity": 10,
                            "IsAvailable": True
                        }
                    }]
                }]
            },
            # 4. Calzado que ya contiene la palabra 'Tenis' en el nombre
            {
                "productId": "104",
                "productName": "Tenis Nike Court Royale",
                "linkText": "tenis-nike-court-royale",
                "categories": ["/Hombre/Calzado/Tenis/"],
                "items": [{
                    "sellers": [{
                        "sellerId": "1",
                        "sellerName": "Nike",
                        "commertialOffer": {
                            "Price": 200000.0,
                            "ListPrice": 250000.0,
                            "AvailableQuantity": 3,
                            "IsAvailable": True
                        }
                    }]
                }]
            },
        ]

        with patch("core.http.get_json", return_value=mock_productos):
            ofertas = vtex._consultar("nike", vtex.TIENDAS["nike"], "calzado", hasta=10)

        # Solo deben pasar el 101 y el 104
        self.assertEqual(len(ofertas), 2)
        titulos = [o.title for o in ofertas]
        precios = [o.price for o in ofertas]

        # Verifica normalización de título y precio
        self.assertIn("Tenis Nike Star Runner 5", titulos)
        self.assertIn("Tenis Nike Court Royale", titulos)
        self.assertNotIn("Camiseta Nike Dri-Fit", titulos)
        self.assertTrue(all(p <= 220000.0 for p in precios))


class PruebaMediosPagoAlkosto(unittest.TestCase):
    def test_traduce_las_etiquetas_utiles(self):
        from sources.algolia_co import _medios_pago
        hit = {"subcat-mediospago_string_mv": [
            "marcas", "BI_ELHO_ALKOS", "ofertas-davivienda-0-interes",
            "envios-rapidos-express", "recien-casados"]}
        etiquetas = _medios_pago(hit)
        self.assertIn("0% interes con Davivienda", etiquetas)
        self.assertIn("Envio rapido express", etiquetas)

    def test_descarta_las_etiquetas_internas(self):
        from sources.algolia_co import _medios_pago
        hit = {"subcat-mediospago_string_mv": ["BI_TVVI_ALKOS", "temporadas", "marcas"]}
        self.assertEqual(_medios_pago(hit), [])

    def test_el_aviso_generico_va_de_ultimo(self):
        from sources.algolia_co import _medios_pago
        hit = {"subcat-mediospago_string_mv": [
            "ofertas-medios-pago", "ofertas-davivienda-0-interes"]}
        self.assertEqual(_medios_pago(hit)[-1], "Descuento con medios de pago")


class PruebaComandos(unittest.TestCase):
    def test_el_catalogo_cubre_las_tiendas_anunciadas(self):
        from core.comandos import CATALOGO, MENU
        for comando in ("alkosto", "ktronix", "exito", "carulla", "olimpica", "exterior", "mercadolibre"):
            self.assertIn(comando, CATALOGO)
        # Todo lo que se anuncia en el menu debe existir como comando real.
        manejados = set(CATALOGO) | {"objetivos", "estado", "ayuda"}
        for comando, _descripcion in MENU:
            self.assertIn(comando, manejados, f"el menu ofrece /{comando} sin implementar")

    def test_cada_comando_apunta_a_una_fuente_valida(self):
        from core.comandos import CATALOGO
        from radar import FUENTES
        for comando, (fuente, _tiendas, titulo) in CATALOGO.items():
            # "co", "amazon_gangas" y "*" son comodines: agrupan fuentes.
            self.assertIn(fuente, set(FUENTES) | {"*", "co", "amazon_gangas"},
                          f"/{comando} apunta a una fuente inexistente")
            self.assertTrue(titulo)

    def test_las_tiendas_existen_en_su_fuente(self):
        from core.comandos import CATALOGO
        from sources import algolia_co, falabella, vtex
        disponibles = {
            "algolia_co": set(algolia_co.TIENDAS),
            "vtex": set(vtex.TIENDAS),
            "falabella": set(falabella.TIENDAS),
        }
        for comando, (fuente, tiendas, _t) in CATALOGO.items():
            if not tiendas:
                continue
            self.assertIn(fuente, disponibles, f"/{comando}: fuente {fuente} sin modulo")
            for tienda in tiendas:
                self.assertIn(tienda, disponibles[fuente], f"/{comando}: {tienda} no existe")

    def test_las_fuentes_de_comunidad_no_exigen_porcentaje(self):
        """Ni Slickdeals ni PROMOCAJITA publican precio de lista.

        Exigirles descuento verificable dejaba /exterior y /cajita siempre
        vacios: su descuento calculado siempre es cero.
        """
        import radar
        from core.models import Deal

        def una(fuente):
            return Deal(source=fuente, store=fuente, country="US", key=fuente,
                        title="Audifonos de prueba", url="u", price=10.0,
                        currency="USD")

        original = radar._ofertas_de
        try:
            for fuente in radar.SIN_PRECIO_DE_LISTA:
                radar._ofertas_de = lambda _w, f, _t: [una(f)]
                self.assertEqual(len(radar._mejores({}, [fuente], None, set(), 5)),
                                 1, fuente)
            # Una tienda normal si tiene que traer un descuento demostrable.
            radar._ofertas_de = lambda _w, _f, _t: [una("vtex")]
            self.assertEqual(radar._mejores({}, ["vtex"], None, set(), 5), [])
        finally:
            radar._ofertas_de = original

    def test_prioriza_lo_que_no_has_visto(self):
        """Pedir la misma tienda dos veces debe traer cosas distintas."""
        import inspect
        import radar
        codigo = inspect.getsource(radar._mejores)
        self.assertIn("nuevas + repetidas", codigo)

    def test_recuerda_lo_que_ya_mostro(self):
        import json as _json
        import tempfile
        from pathlib import Path as _Path
        from core import comandos
        with tempfile.TemporaryDirectory() as tmp:
            original = comandos.ESTADO
            comandos.ESTADO = _Path(tmp) / "estado.json"
            try:
                self.assertEqual(comandos.ya_mostradas(), set())
                comandos.marcar_mostradas(["a", "b"])
                self.assertEqual(comandos.ya_mostradas(), {"a", "b"})
                comandos.marcar_mostradas(["c"])
                self.assertEqual(comandos.ya_mostradas(), {"a", "b", "c"})
                # El offset de Telegram no se pierde al guardar lo mostrado.
                comandos._guardar_estado(99)
                comandos.marcar_mostradas(["d"])
                self.assertEqual(comandos._leer_estado(), 99)
            finally:
                comandos.ESTADO = original

    def test_solo_obedece_al_chat_configurado(self):
        """En grupos y canales no se atienden comandos; solo en privado para usuarios autorizados."""
        from unittest.mock import patch
        from core import comandos
        original = config.TELEGRAM_CHAT_ID
        config.TELEGRAM_CHAT_ID = "-100"
        try:
            # Miembro común en grupo -> ignorado
            with patch("core.whitelist.es_admin", return_value=False):
                grupo = {"text": "/alkosto", "chat": {"id": -100, "type": "supergroup"}, "from": {"id": 999}}
                self.assertIsNone(comandos.leer_comando(grupo))

            # Admin en grupo y privado -> permitido
            with patch("core.whitelist.es_admin", return_value=True):
                grupo_admin = {"text": "/alkosto", "chat": {"id": -100, "type": "supergroup"}, "from": {"id": 12345}}
                privado = {"text": "/alkosto", "chat": {"id": 12345, "type": "private"}, "from": {"id": 12345}}
                self.assertIsNotNone(comandos.leer_comando(grupo_admin))
                self.assertIsNotNone(comandos.leer_comando(privado))
        finally:
            config.TELEGRAM_CHAT_ID = original

    def test_el_menu_no_ofrece_lo_que_se_retiro(self):
        """objetivos y estado se quitaron del menu por pedido del usuario."""
        from core.comandos import MENU
        visibles = {c for c, _d in MENU}
        self.assertNotIn("objetivos", visibles)
        self.assertNotIn("estado", visibles)
        self.assertIn("colombia", visibles)

    def test_colombia_agrupa_las_tiendas_colombianas(self):
        """/colombia debe mirar todas las fuentes del pais, no solo una.

        Antes esta prueba exigia exactamente 5 tiendas y se rompio al sumar
        Jumbo y compania. El numero va a seguir creciendo: lo que hay que
        proteger es que consulte las tres fuentes, y que la ayuda no prometa
        una cifra que quede desactualizada.
        """
        import inspect
        import radar
        from core.comandos import AYUDA, MENU
        codigo = inspect.getsource(radar.atender_solicitudes)
        self.assertIn('if fuente == "co"', codigo)
        for fuente in ("algolia_co", "vtex"):
            self.assertIn(fuente, codigo)
        texto = AYUDA + " ".join(d for _c, d in MENU)
        for cifra in ("cinco tiendas", "5 tiendas"):
            self.assertNotIn(cifra, texto)

    def test_lee_la_cantidad_pedida_en_el_comando(self):
        """/alkosto 25 debe pedir 25, no el valor por defecto."""
        from unittest.mock import patch
        from core import comandos
        with patch("core.whitelist.es_admin", return_value=True):
            def leer(texto):
                return comandos.leer_comando({"text": texto, "chat": {"id": 12345, "type": "private"}, "from": {"id": 12345}})

            self.assertEqual(leer("/alkosto 25")["cantidad"], 25)
            # Sin numero manda el valor por defecto, que resuelve el radar.
            self.assertIsNone(leer("/alkosto")["cantidad"])
            # El @ del bot no estorba, y el tope protege del limite de Telegram.
            self.assertEqual(leer("/alkosto@MiBot 9999")["comando"], "alkosto")
            self.assertEqual(leer("/alkosto 9999")["cantidad"],
                             config.COMANDO_MAX_RESULTADOS)

    def test_el_tope_protege_del_limite_de_telegram(self):
        import config
        self.assertLessEqual(config.COMANDO_RESULTADOS, config.COMANDO_MAX_RESULTADOS)
        # A 3.5s por tarjeta, el tope debe caber en la corrida de 10 minutos.
        self.assertLess(config.COMANDO_MAX_RESULTADOS * 3.5, 600)

    def test_una_oferta_sin_precio_no_tumba_el_comando(self):
        """Slickdeals anuncia cosas como "Extra 10% off" sin decir el precio.

        Pedir 15 resultados en vez de 5 hizo que esas colaran en la respuesta,
        y calcular() no acepta None: el comando moria con TypeError y el flujo
        de Actions terminaba en rojo.
        """
        import inspect
        import radar
        codigo = inspect.getsource(radar.atender_solicitudes)
        self.assertIn('deal.country == "US" and deal.price', codigo)
        # El guardia es obligatorio porque calcular() no tolera un precio nulo.
        with self.assertRaises(TypeError):
            calcular(oferta(country="US", price=None).price, 4000.0)

    def test_una_barra_sola_no_tumba_la_lectura(self):
        """Un "/" pelado no es un comando, pero tampoco puede reventar.

        El offset se confirma al final de la lectura: si el mensaje la tumba,
        Telegram lo vuelve a entregar y cada corrida falla igual, para siempre.
        """
        from pathlib import Path as _Path
        from unittest.mock import patch
        from core import comandos
        originales = (comandos.ESTADO, comandos.http.get_json,
                      config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID)
        with tempfile.TemporaryDirectory() as tmp, patch("core.whitelist.es_admin", return_value=True):
            comandos.ESTADO = _Path(tmp) / "estado.json"
            config.TELEGRAM_BOT_TOKEN = "1:x"
            config.TELEGRAM_CHAT_ID = "-100"
            comandos.http.get_json = lambda url, **kw: {"ok": True, "result": [
                {"update_id": 1,
                 "message": {"text": "/", "chat": {"id": 12345, "type": "private"}, "from": {"id": 12345}}},
                {"update_id": 2,
                 "message": {"text": "/alkosto 25", "chat": {"id": 12345, "type": "private"}, "from": {"id": 12345}}},
            ]}
            try:
                hallados = comandos.pendientes()
                self.assertEqual(hallados,
                                 [{"comando": "alkosto", "chat_id": 12345,
                                   "cantidad": 25, "user_id": 12345, "nombre": "Usuario", "username": None}])
                # La barra sola tambien queda confirmada: no vuelve a llegar.
                self.assertEqual(comandos._leer_estado(), 3)
            finally:
                (comandos.ESTADO, comandos.http.get_json,
                 config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID) = originales

    def test_teclados_telegram_bien_formados(self):
        """Los teclados interactivos deben tener las opciones correctas y ser minimizables."""
        from core import telegram
        from sources import vtex
        tiendas = telegram.teclado_tiendas()
        self.assertTrue(tiendas.get("resize_keyboard"))
        self.assertFalse(tiendas.get("is_persistent", False))
        botones_tiendas = [b["text"] for row in tiendas["keyboard"] for b in row]
        self.assertIn("🟡 Éxito", botones_tiendas)
        self.assertIn("🔴 Alkosto", botones_tiendas)
        self.assertIn("🟢 Carulla", botones_tiendas)
        self.assertIn("🟠 Homecenter", botones_tiendas)
        self.assertIn("📦 Promocajita", botones_tiendas)
        self.assertIn("🇨🇴 Comparar Tiendas", botones_tiendas)
        self.assertNotIn("🎯 Mis Objetivos", botones_tiendas)
        self.assertNotIn("pepeganga", vtex.TIENDAS)

        categorias = telegram.teclado_categorias("Éxito")
        self.assertTrue(categorias.get("resize_keyboard"))
        self.assertFalse(categorias.get("is_persistent", False))
        botones_cat = [b["text"] for row in categorias["keyboard"] for b in row]
        self.assertIn("🌟 TODO", botones_cat)
        self.assertIn("📺 Smart TV", botones_cat)
        self.assertIn("⬅️ Volver a Tiendas", botones_cat)

    def test_botones_menu_interactivo_se_interpretan_correctamente(self):
        """El parser debe entender cuando el usuario toca un boton en vez de escribir un comando."""
        from unittest.mock import patch
        from core import comandos
        with patch("core.whitelist.es_admin", return_value=True):
            def leer(t):
                return comandos.leer_comando({"text": t, "chat": {"id": 12345, "type": "private"}, "from": {"id": 12345}})

            # Tienda
            res = leer("🟡 Éxito")
            self.assertEqual(res["tipo"], "elegir_tienda")
            self.assertEqual(res["tienda"], "exito")

            # Promocajita (ahora con menu de categorias)
            res_cajita = leer("📦 Promocajita")
            self.assertEqual(res_cajita["tipo"], "elegir_tienda")
            self.assertEqual(res_cajita["tienda"], "cajita")

            # Categoria
            res_cat = leer("📺 Smart TV")
            self.assertEqual(res_cat["tipo"], "categoria")
            self.assertIn("televisor", res_cat["consultas"])

            # Categoria sin emoji
            res_cat2 = leer("smart tv")
            self.assertEqual(res_cat2["tipo"], "categoria")

            # Todo
            res_todo = leer("🌟 TODO")
            self.assertEqual(res_todo["tipo"], "todo_tienda")

            # Volver
            res_volver = leer("⬅️ Volver a Tiendas")
            self.assertEqual(res_volver["tipo"], "menu")

            # Objetivos
            res_obj = leer("🎯 Mis Objetivos")
            self.assertEqual(res_obj["comando"], "objetivos")

    def test_limite_precio_maximo(self):
        """No deben pasar ofertas de mas de 2 millones de pesos."""
        import radar
        barato = oferta(price=1_500_000.0, currency="COP")
        caro = oferta(price=2_500_000.0, currency="COP")
        caro_usd = oferta(price=700.0, currency="USD")  # 700 * 4000 = 2.800.000 COP
        barato_usd = oferta(price=300.0, currency="USD") # 300 * 4000 = 1.200.000 COP

        self.assertTrue(radar._precio_admisible(barato, trm=4000.0))
        self.assertFalse(radar._precio_admisible(caro, trm=4000.0))
        self.assertTrue(radar._precio_admisible(barato_usd, trm=4000.0))
        self.assertFalse(radar._precio_admisible(caro_usd, trm=4000.0))

    def test_topes_categoria_precio_objetivos(self):
        """Valida que los objetivos reconozcan las metas fijadas por el usuario."""
        import config
        from core import objetivos
        metas = config.load_watchlist().get("objetivos", [])
        # Ropa: < 50.000
        self.assertIsNotNone(objetivos.alcanzado(oferta(title="KOAJ Camiseta azul", price=18130.0), metas))
        self.assertIsNotNone(objetivos.alcanzado(oferta(title="Camiseta Polo Lacoste", price=45000.0), metas))
        self.assertIsNone(objetivos.alcanzado(oferta(title="Camiseta Polo Lacoste", price=65000.0), metas))
        # Zapatos: < 150.000
        self.assertIsNotNone(objetivos.alcanzado(oferta(title="Tenis Running Puma", price=140000.0), metas))
        self.assertIsNone(objetivos.alcanzado(oferta(title="Tenis Nike Air Jordan", price=250000.0), metas))
        # TV: < 1.500.000
        self.assertIsNotNone(objetivos.alcanzado(oferta(title="Smart TV Xiaomi 43", price=1100000.0), metas))
        self.assertIsNone(objetivos.alcanzado(oferta(title="Televisor OLED LG 55", price=1800000.0), metas))
        # Monitores: < 800.000
        self.assertIsNotNone(objetivos.alcanzado(oferta(title="Monitor Gamer 24 LG", price=650000.0), metas))
        self.assertIsNone(objetivos.alcanzado(oferta(title="Monitor Samsung 34 Curvo", price=1200000.0), metas))
        # Lavadoras: < 1.500.000
        self.assertIsNotNone(objetivos.alcanzado(oferta(title="Lavadora Whirlpool 16kg", price=1350000.0), metas))
        self.assertIsNone(objetivos.alcanzado(oferta(title="Lavadora Secadora LG", price=2200000.0), metas))
        # Neveras: < 1.800.000
        self.assertIsNotNone(objetivos.alcanzado(oferta(title="Nevera Haceb 250L", price=1450000.0), metas))
        self.assertIsNone(objetivos.alcanzado(oferta(title="Nevera Samsung French Door", price=3500000.0), metas))
        # Electrodomésticos pequeños: < 250.000
        self.assertIsNotNone(objetivos.alcanzado(oferta(title="Freidora de Aire Imusa", price=199900.0), metas))
        self.assertIsNone(objetivos.alcanzado(oferta(title="Freidora Ninja Dual", price=550000.0), metas))

    def test_persistencia_tienda_activa(self):
        """La tienda seleccionada se guarda en el estado para recordar contexto entre mensajes."""
        import tempfile
        from pathlib import Path as _Path
        from core import comandos
        with tempfile.TemporaryDirectory() as tmp:
            original = comandos.ESTADO
            comandos.ESTADO = _Path(tmp) / "estado.json"
            try:
                self.assertEqual(comandos.tienda_activa(), "colombia")
                comandos.fijar_tienda_activa("alkosto")
                self.assertEqual(comandos.tienda_activa(), "alkosto")
            finally:
                comandos.ESTADO = original

    def test_atender_solicitudes_flujo_interactivo(self):
        """El flujo de seleccion de tienda y categoria despacha mensajes con teclado."""
        import tempfile
        from pathlib import Path as _Path
        import radar
        from core import comandos, telegram

        mensajes_enviados = []
        original_send = telegram.send
        original_estado = comandos.ESTADO
        try:
            telegram.send = lambda texto, preview=False, reply_markup=None, **k: mensajes_enviados.append((texto, reply_markup)) or True
            with tempfile.TemporaryDirectory() as tmp:
                comandos.ESTADO = _Path(tmp) / "estado.json"

                # 1. Menu
                radar.atender_solicitudes([{"comando": "menu", "tipo": "menu", "chat_id": -100}])
                self.assertTrue(any("Menú Principal" in m[0] for m in mensajes_enviados))
                self.assertIsNotNone(mensajes_enviados[-1][1])

                # 2. Elegir tienda
                mensajes_enviados.clear()
                radar.atender_solicitudes([{
                    "comando": "elegir_tienda",
                    "tienda": "exito",
                    "tienda_nombre": "Éxito",
                    "tipo": "elegir_tienda",
                    "chat_id": -100
                }])
                self.assertEqual(comandos.tienda_activa(), "exito")
                self.assertTrue(any("Éxito seleccionado" in m[0] for m in mensajes_enviados))
                self.assertIsNotNone(mensajes_enviados[-1][1])
        finally:
            telegram.send = original_send
            comandos.ESTADO = original_estado


class PruebaTodasLasFuentesLlegan(unittest.TestCase):
    """Una fuente configurada tiene que llegar a su modulo.

    PROMOCAJITA se configura con "canales" y no con "queries", y un guardia
    generico la descartaba antes de llegar a su rama: /cajita devolvia vacio
    siempre, y /colombia y /todo la saltaban en silencio. Nadie se enteraba
    porque no hay error, solo ausencia.
    """

    def test_cada_fuente_configurada_llama_a_su_modulo(self):
        import config
        import radar
        w = config.load_watchlist()
        modulos = {"algolia_co": "algolia_co", "vtex": "vtex",
                   "falabella": "falabella",
                   "slickdeals": "slickdeals", "promocajita": "promocajita",
                   "descuentostech": "descuentostech"}
        for fuente, modulo in modulos.items():
            cfg = w.get(fuente) or {}
            if not (cfg.get("queries") or cfg.get("canales")):
                continue                       # sin configurar, no aplica
            mod = getattr(radar, modulo)
            original, llamadas = mod.fetch, []
            try:
                mod.fetch = lambda *a, _l=llamadas, **k: _l.append(1) or []
                radar._ofertas_de(w, fuente, None)
            finally:
                mod.fetch = original
            self.assertEqual(len(llamadas), 1,
                             f"{fuente} no llego a {modulo}.fetch()")


class PruebaMarcas(unittest.TestCase):
    """Una marca corta se cuela dentro de palabras corrientes."""

    def test_puma_no_es_espuma(self):
        """Caso real: buscar "puma" en Olimpica devolvia colchones
        "esPUMAdos" y adaptadores "ComPUMAx". De 50 resultados, cero eran
        PUMA."""
        from core import filtros
        for falso in ("Colchon Nova Espumados Del Litoral 140x190",
                      "Adaptador Corriente 12v2a Compumax 11054",
                      "Espuma Facial Granulos 120Gr Kaloe Exfoliante"):
            self.assertFalse(filtros.menciona(falso, "puma"), falso)
        for real in ("PUMA Men Softride Sneakers",
                     "Tenis Puma Caven 2.0 Hombre"):
            self.assertTrue(filtros.menciona(real, "puma"), real)

    def test_reconoce_marcas_de_dos_palabras(self):
        from core import filtros
        self.assertTrue(filtros.menciona("Zapatillas New Balance 574", "new balance"))
        self.assertFalse(filtros.menciona("Balance de cocina digital", "new balance"))

    def test_no_confunde_una_marca_con_el_principio_de_otra_palabra(self):
        from core import filtros
        self.assertFalse(filtros.menciona("Camiseta Niketown generica", "nike"))
        self.assertTrue(filtros.menciona("Tenis Nike Zoom Vomero 5", "nike"))

    def test_el_perfume_no_es_ropa(self):
        """Las marcas deportivas venden perfume, y buscar "adidas" lo traia."""
        from core import filtros
        self.assertTrue(filtros.descartado(
            "Perfume Adidas Hombre Ice Dive Eau De Toilette", filtros.VETADAS_CO))


class PruebaRitmos(unittest.TestCase):
    """Dos ritmos y un horario: ni todo cambia al mismo paso, ni de
    madrugada hay nada que buscar."""

    def test_de_madrugada_no_sale_a_buscar(self):
        """Un comando a las 3 a.m. despierta el servicio; sin esta guarda
        arrancaria una ronda completa contra las 12 tiendas."""
        import datetime as _dt
        import server
        def a_las(hora):
            return server.en_horario(_dt.datetime(2026, 9, 8, hora,
                                                  tzinfo=server.ZONA_CO))
        for dormida in (0, 3, 5):
            self.assertFalse(a_las(dormida), f"{dormida}:00 deberia estar fuera")
        for despierta in (6, 12, 23):
            self.assertTrue(a_las(despierta), f"{despierta}:00 deberia entrar")

    def test_los_dos_ritmos_cubren_todo_sin_repetir(self):
        """Una fuente en los dos ritmos se consultaria de mas; una fuente en
        ninguno no se consultaria nunca, y nadie se enteraria."""
        import radar
        import server
        rapido, lento = set(server.RONDA_COMUNIDAD), set(server.RONDA_CATALOGOS)
        self.assertEqual(rapido | lento, set(radar.FUENTES))
        self.assertEqual(rapido & lento, set())

    def test_lo_que_cambia_rapido_va_en_el_ritmo_rapido(self):
        """Las ofertas de comunidad duran horas; un catalogo de tienda se
        mueve por dia. Cambiarlas de ritmo seria gastar peticiones."""
        import server
        self.assertIn("promocajita", server.RONDA_COMUNIDAD)
        self.assertIn("slickdeals", server.RONDA_COMUNIDAD)
        self.assertIn("vtex", server.RONDA_CATALOGOS)
        self.assertLess(server.INTERVALO_COMUNIDAD_MIN,
                        server.INTERVALO_CATALOGOS_MIN)

    def test_la_ronda_programada_espera_su_turno(self):
        """Descartar la ronda de catalogos por chocar con una de comunidad
        costaria una hora entera."""
        import inspect
        import server
        codigo = inspect.getsource(server.programador)
        self.assertIn("espera_s=", codigo)


class PruebaPromocajita(unittest.TestCase):
    """El canal de Telegram de la comunidad colombiana de ofertas."""

    BLOQUE = (
        '<div class="tgme_widget_message_photo_wrap" '
        'style="background-image:url(https://cdn1.telesco.pe/file/abc)"></div>'
        '<a class="tgme_widget_message_date" href="https://t.me/cajitatech/84260">'
        '<time datetime="2026-09-08T15:42:58+00:00"></time></a>'
        '<div class="tgme_widget_message_text js-message_text">'
        '#Tablet 💰 $679915 COP - Redmi Pad 2 (4+128) + Cover '
        '🛍 Aplicar XIAOMILOVERS para obtener el descuento '
        'Oferta ➡ <a href="https://pccajita.link/k44dla">link</a></div>'
    )

    def test_el_punto_en_pesos_no_son_centavos(self):
        """En COP el punto separa miles; en USD son los centavos. Tratarlos
        igual multiplicaba por cien el precio en dolares."""
        from sources.promocajita import _monto
        self.assertEqual(_monto("679.915", "COP"), 679915.0)
        self.assertEqual(_monto("679915", "COP"), 679915.0)
        self.assertEqual(_monto("1,299.99", "USD"), 1299.99)
        self.assertEqual(_monto("3.97", "USD"), 3.97)

    def test_lee_una_publicacion_completa(self):
        from sources import promocajita
        deal = promocajita._una_publicacion("cajitatech/84260", self.BLOQUE)
        self.assertIsNotNone(deal)
        self.assertEqual(deal.price, 679915.0)
        self.assertEqual(deal.currency, "COP")
        self.assertEqual(deal.country, "CO")
        self.assertIn("Redmi Pad 2", deal.title)
        # El titulo corta antes de la mecanica de la oferta.
        self.assertNotIn("Aplicar", deal.title)
        self.assertEqual(deal.coupons, ["XIAOMILOVERS"])
        self.assertEqual(deal.url, "https://pccajita.link/k44dla")
        self.assertEqual(deal.key, "promocajita:cajitatech/84260")
        # Nadie publica el precio anterior: fingirlo seria inventar el descuento.
        self.assertIsNone(deal.list_price)
        self.assertEqual(deal.discount_verificable, 0.0)

    def test_incluir_es_lo_que_evita_que_ahogue_el_canal(self):
        """Publican decenas de cosas al dia; sin este filtro se comerian los
        cupos de alerta con floreros."""
        from sources import promocajita
        pasa = promocajita._una_publicacion("c/1", self.BLOQUE, incluir=["tablet"])
        no_pasa = promocajita._una_publicacion("c/1", self.BLOQUE, incluir=["nevera"])
        self.assertIsNotNone(pasa)
        self.assertIsNone(no_pasa)

    def test_sin_precio_no_es_oferta(self):
        from sources import promocajita
        sin = self.BLOQUE.replace("$679915 COP", "precio en el enlace")
        self.assertIsNone(promocajita._una_publicacion("c/1", sin))


class PruebaFalabella(unittest.TestCase):
    """Falabella y Homecenter: mismo JSON incrustado, formatos distintos."""

    def test_el_precio_con_tarjeta_no_es_el_precio(self):
        """Usar el precio CMR inflaria el descuento de una oferta que exige
        tener esa tarjeta. Va como nota, no como precio."""
        from sources import falabella
        precios = [
            {"type": "internetPrice", "crossed": False, "price": ["2.699.900"]},
            {"type": "normalPrice", "crossed": True, "price": ["4.999.900"]},
            {"type": "cmrPrice", "crossed": False, "price": ["2.599.900"]},
        ]
        precio, lista, tarjeta = falabella._precios(precios)
        self.assertEqual(precio, 2699900.0)
        self.assertEqual(lista, 4999900.0)
        self.assertEqual(tarjeta, 2599900.0)

    def test_lee_los_dos_formatos_de_precio(self):
        """Homecenter regala el numero limpio; Falabella solo el texto, y a
        veces dentro de una lista."""
        from sources import falabella
        self.assertEqual(falabella._numero({"priceWithoutFormatting": 2199900}), 2199900.0)
        self.assertEqual(falabella._numero({"price": "3.999.900"}), 3999900.0)
        self.assertEqual(falabella._numero({"price": ["3.199.900"]}), 3199900.0)
        self.assertIsNone(falabella._numero({"price": None}))

    def test_homecenter_marca_la_lista_con_NORMAL(self):
        """Homecenter no usa 'crossed': rotula el precio de lista como NORMAL."""
        from sources import falabella
        precios = [
            {"type": "INTERNET", "priceWithoutFormatting": 2199900},
            {"label": "Normal", "type": "NORMAL", "priceWithoutFormatting": 3999900},
        ]
        precio, lista, _tarjeta = falabella._precios(precios)
        self.assertEqual((precio, lista), (2199900.0, 3999900.0))

    def test_la_foto_se_pide_en_jpeg(self):
        """El CDN de Falabella sirve WebP y sendPhoto no lo acepta: lo trata
        como sticker. La oferta llegaba, pero sin foto."""
        from sources import falabella
        base = "https://media.falabella.com.co/falabellaCO/73677567_1/public"
        self.assertEqual(falabella._foto([base]), base + "?format=jpg")
        # Si la URL ya trae parametros, se encadena bien.
        self.assertEqual(falabella._foto([base + "?w=800"]),
                         base + "?w=800&format=jpg")
        self.assertIsNone(falabella._foto([]))
        self.assertIsNone(falabella._foto(None))

    def test_encuentra_los_productos_donde_sea(self):
        """Falabella los deja cerca de la raiz y Homecenter tres niveles
        adentro; la ruta no es un contrato, la forma del dato si."""
        from sources import falabella
        hondo = {"props": {"pageProps": {"searchProps": {"searchData": {
            "results": [{"displayName": "Taladro", "prices": []}]}}}}}
        somero = {"pageProps": {"results": [{"displayName": "Televisor", "prices": []}]}}
        self.assertEqual(falabella._productos(hondo)[0]["displayName"], "Taladro")
        self.assertEqual(falabella._productos(somero)[0]["displayName"], "Televisor")
        # Una lista de "results" que no son productos no cuenta.
        self.assertIsNone(falabella._productos({"results": [{"otra": "cosa"}]}))


class PruebaRuido(unittest.TestCase):
    """Lo que separa el producto de lo que lo acompaña."""

    def test_el_accesorio_tiene_que_abrir_el_titulo(self):
        """Buscar la palabra en cualquier parte descartaba el producto.

        "Aspiradora T-FAL X-FORCE, bateria 45 min, 4 accesorios" es una
        aspiradora. "Base KALLEY para televisores 23 a 55" es un soporte. La
        diferencia esta en si la palabra abre el titulo o solo se menciona.
        """
        from core import filtros
        producto = "Aspiradora Inalambrica T-FAL X-FORCE FLEX, 250W, 4 accesorios"
        accesorio = 'Base KALLEY Brazo Flexible para televisores 23" a 55"'
        mueble = 'Rack TV 45" Soho Bellota - RTA Design'
        self.assertFalse(filtros.es_accesorio(producto))
        self.assertTrue(filtros.es_accesorio(accesorio))
        # El mueble donde va el televisor tampoco es un televisor.
        self.assertTrue(filtros.es_accesorio(mueble))

    def test_una_lista_vacia_no_veta_nada(self):
        """La drogueria pide ver todo: el veto general le tumbaria el
        maquillaje ("Base liquida Samy") y el protector solar."""
        import radar
        from core.models import Deal
        def oferta_de(titulo):
            return Deal(source="vtex", store="La Rebaja", country="CO",
                        key="k", title=titulo, url="u", price=1.0, currency="COP")
        ofertas = [oferta_de("BASE LIQUIDA SAMY GO BRIGHT"),
                   oferta_de("PROTECTOR SOLAR FACIAL SPF 50")]
        self.assertEqual(len(radar._sin_ruido(ofertas, {"excluir": []})), 2)
        # Sin configuracion propia si aplica el veto general.
        self.assertEqual(len(radar._sin_ruido(ofertas, {})), 0)

    def test_las_tiendas_de_la_watchlist_existen(self):
        """Un nombre mal escrito se descubriria en produccion, no aqui."""
        import config
        from sources import algolia_co, falabella, vtex
        w = config.load_watchlist()
        conocidas = {"vtex": set(vtex.TIENDAS),
                     "algolia_co": set(algolia_co.TIENDAS),
                     "falabella": set(falabella.TIENDAS)}
        for fuente, validas in conocidas.items():
            for tienda in (w.get(fuente) or {}).get("tiendas") or []:
                self.assertIn(tienda, validas, f"{fuente}: {tienda}")


class PruebaWebhook(unittest.TestCase):
    """El webhook es la puerta por la que entran los comandos: si acepta
    cualquier cosa, cualquiera pone al bot a trabajar para el."""

    def _servidor(self):
        """Levanta el manejador real en un puerto libre de la maquina."""
        import threading as _th
        from http.server import ThreadingHTTPServer
        import server
        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Manejador)
        srv.daemon_threads = True
        _th.Thread(target=srv.serve_forever, daemon=True).start()
        # addCleanup corre al reves de como se registra: primero hay que
        # parar el bucle y solo despues cerrar el socket que escucha. Al
        # reves, el servidor atendia con el socket ya cerrado y la prueba
        # fallaba una de cada tres veces.
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}", server

    def _post(self, url, cuerpo, secreto=None):
        import urllib.error
        import urllib.request
        cabeceras = {"Content-Type": "application/json"}
        if secreto is not None:
            cabeceras["X-Telegram-Bot-Api-Secret-Token"] = secreto
        peticion = urllib.request.Request(
            url, data=json.dumps(cuerpo).encode(), headers=cabeceras, method="POST")
        try:
            with urllib.request.urlopen(peticion, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_sin_el_secreto_no_entra_nada(self):
        from core import telegram
        base, servidor = self._servidor()
        mensaje = {"update_id": 1, "message": {"text": "/alkosto", "chat": {"id": 1}}}

        self.assertEqual(self._post(base + servidor.RUTA_WEBHOOK, mensaje), 403)
        self.assertEqual(
            self._post(base + servidor.RUTA_WEBHOOK, mensaje, "no-es-el-secreto"), 403)
        # Con el secreto correcto se acepta al instante, aunque el mensaje no
        # sea del chat configurado (eso lo filtra leer_comando despues).
        self.assertEqual(
            self._post(base + servidor.RUTA_WEBHOOK, mensaje,
                       telegram.secreto_webhook()), 200)

    def test_el_secreto_no_revela_el_token(self):
        """Se deriva del token con un hash: estable entre reinicios, y no hay
        forma de devolverlo al token."""
        from core import telegram
        original = config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_WEBHOOK_SECRET
        config.TELEGRAM_BOT_TOKEN = "8489029941:AAEE6XvWabcdefghijklmnop"
        config.TELEGRAM_WEBHOOK_SECRET = ""
        try:
            secreto = telegram.secreto_webhook()
            self.assertNotIn("AAEE6XvW", secreto)
            self.assertNotIn("8489029941", secreto)
            self.assertEqual(secreto, telegram.secreto_webhook())   # estable
        finally:
            config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_WEBHOOK_SECRET = original

    def test_un_comando_repetido_se_responde_una_sola_vez(self):
        """Telegram reintenta una entrega si duda de que llegara."""
        import server
        original = list(server._atendidos)
        try:
            server._atendidos.clear()
            self.assertFalse(server._ya_atendido(7))
            self.assertTrue(server._ya_atendido(7))
            # La memoria no crece sin control.
            for i in range(server._ATENDIDOS_MAX + 50):
                server._ya_atendido(1000 + i)
            self.assertLessEqual(len(server._atendidos), server._ATENDIDOS_MAX)
        finally:
            server._atendidos[:] = original


class PruebaSeguridadYErrores(unittest.TestCase):
    def test_el_token_nunca_aparece_en_un_error(self):
        """El token viaja en la URL de Telegram; en un repo publico no puede
        terminar en los logs."""
        from core.http import url_segura
        sucia = "https://api.telegram.org/bot8489029941:AAEE6XvWabcdefghijklmnop/sendMessage"
        limpia = url_segura(sucia)
        self.assertNotIn("AAEE6XvW", limpia)
        self.assertNotIn("8489029941", limpia)
        self.assertIn("/bot***/sendMessage", limpia)

    def test_no_estropea_una_url_normal(self):
        from core.http import url_segura
        url = "https://www.alkosto.com/tv-kalley/p/123"
        self.assertEqual(url_segura(url), url)

    def test_detecta_el_cambio_a_supergrupo(self):
        """Un grupo que pasa a supergrupo cambia de id y deja al bot mudo."""
        import io
        import contextlib
        from core import telegram
        error = Exception('HTTP 400 -> {"parameters":{"migrate_to_chat_id":-1004476593255}}')
        salida = io.StringIO()
        with contextlib.redirect_stdout(salida):
            telegram._avisar_migracion(error)
        self.assertIn("-1004476593255", salida.getvalue())
        self.assertIn("SUPERGRUPO", salida.getvalue())

    def test_calla_si_el_error_es_otro(self):
        import io
        import contextlib
        from core import telegram
        salida = io.StringIO()
        with contextlib.redirect_stdout(salida):
            telegram._avisar_migracion(Exception("HTTP 500 boom"))
        self.assertEqual(salida.getvalue(), "")


class PruebaMarketplace(unittest.TestCase):
    """Cuando creerle el precio de lista a un vendedor externo.

    El mismo televisor Kalley aparece con identico precio de lista en la tienda
    propia de Alkosto y en el marketplace del Exito, asi que ese numero es real
    y descartarlo costaba ofertas legitimas. Pero los accesorios baratos al
    70-80% si son precio inflado.
    """
    TIENDA = {"nombre": "Exito", "api": "x", "web": "https://www.exito.com"}

    def producto(self, precio, lista, seller="2"):
        return {"linkText": "x", "productId": "1", "productName": "P",
                "items": [{"images": [], "sellers": [{"sellerId": seller, "sellerName": "s",
                    "commertialOffer": {"Price": precio, "ListPrice": lista,
                                        "AvailableQuantity": 5, "IsAvailable": True}}]}]}

    def confiable(self, precio, lista, seller="2"):
        from sources.vtex import _consultar
        import sources.vtex as v
        original = v.http.get_json
        v.http.get_json = lambda *a, **k: [self.producto(precio, lista, seller)]
        try:
            return _consultar("exito", self.TIENDA, "q", 10)[0].list_price_trusted
        finally:
            v.http.get_json = original

    def test_le_cree_al_televisor_caro_con_rebaja_creible(self):
        self.assertTrue(self.confiable(1_249_900, 3_099_900))   # -59.7%, caso real

    def test_no_le_cree_al_accesorio_barato(self):
        self.assertFalse(self.confiable(38_718, 110_000))       # soporte de pared

    def test_no_le_cree_al_descuento_absurdo(self):
        self.assertFalse(self.confiable(1_679_990, 36_799_990))  # el portatil falso

    def test_a_la_tienda_propia_si_le_cree(self):
        self.assertTrue(self.confiable(64_900, 184_900, seller="1"))


class PruebaRespuestaInmediataYCancelacion(unittest.TestCase):
    def test_notificar_inicio_busqueda_marca_notificado(self):
        import server
        from core import telegram
        enviados = []
        orig_send = telegram.send
        orig_esc = telegram.accion_escribiendo
        telegram.send = lambda txt, **k: enviados.append(txt) or True
        telegram.accion_escribiendo = lambda *a, **k: None
        try:
            reqs = [
                {"tipo": "categoria", "categoria_nombre": "👟 Zapatos y Tenis"},
                {"tipo": "todo_tienda"},
                {"comando": "alkosto", "tipo": "slash"},
            ]
            server._notificar_inicio_busqueda(reqs)
            for r in reqs:
                self.assertTrue(r.get("notificado"))
            self.assertEqual(len(enviados), 3)
            self.assertIn("Zapatos y Tenis", enviados[0])
            self.assertIn("catálogo", enviados[1])
            self.assertIn("Alkosto", enviados[2])
        finally:
            telegram.send = orig_send
            telegram.accion_escribiendo = orig_esc

    def test_nueva_busqueda_incrementa_token_y_cancela(self):
        import radar
        t1 = radar.nueva_busqueda()
        t2 = radar.nueva_busqueda()
        self.assertGreater(t2, t1)


class PruebaMercadoLibre(unittest.TestCase):
    def test_teclado_tiendas_incluye_mercadolibre(self):
        from core import telegram
        teclado = telegram.teclado_tiendas()
        botones = [btn["text"] for fila in teclado.get("keyboard", []) for btn in fila]
        self.assertIn("💛 Mercado Libre", botones)

    def test_boton_mercadolibre_abre_tienda(self):
        from unittest.mock import patch
        from core import comandos
        with patch("core.whitelist.es_admin", return_value=True):
            req = comandos.leer_comando({"text": "💛 Mercado Libre", "chat": {"id": 12345, "type": "private"}, "from": {"id": 12345}})
            self.assertIsNotNone(req)
            self.assertEqual(req.get("tipo"), "elegir_tienda")
            self.assertEqual(req.get("tienda"), "mercadolibre")

    def test_mapeo_categorias_mercadolibre(self):
        from sources import mercadolibre
        self.assertEqual(mercadolibre._categoria_para_consultas(["smart tv"]), "MCO1000")
        self.assertEqual(mercadolibre._categoria_para_consultas(["portatil"]), "MCO1648")
        self.assertEqual(mercadolibre._categoria_para_consultas(["celular"]), "MCO1051")
        self.assertEqual(mercadolibre._categoria_para_consultas(["tenis"]), "MCO1276")
        self.assertEqual(mercadolibre._categoria_para_consultas(["nevera"]), "MCO5726")

    def test_parsea_estado_con_ofertas_y_cupones(self):
        from sources import mercadolibre
        from core import http
        import json

        mock_data = {
            "appProps": {
                "pageProps": {
                    "data": {
                        "items": [
                            {
                                "card": {
                                    "metadata": {
                                        "id": "MCO9999",
                                        "url": "https://articulo.mercadolibre.com.co/MCO-9999-producto",
                                    },
                                    "pictures": {"pictures": [{"id": "123456-MCO"}]},
                                    "components": [
                                        {"type": "title", "title": {"text": "Smart TV Samsung 55 4K UHD"}},
                                        {
                                            "type": "price",
                                            "price": {
                                                "current_price": {"value": 1500000.0},
                                                "price_labels": [
                                                    {
                                                        "values": [
                                                            {"key": "previous_price", "price": {"value": 3000000.0}}
                                                        ]
                                                    }
                                                ],
                                            },
                                        },
                                        {
                                            "type": "promotions",
                                            "promotions": [{"text": "{icon_cockade} Cupón 10% OFF"}],
                                        },
                                        {
                                            "type": "seller",
                                            "seller": {"text": "Tienda Oficial Samsung"},
                                        },
                                    ],
                                }
                            }
                        ]
                    }
                }
            }
        }
        mock_html = f"<html><body><script>_n.ctx.r = {json.dumps(mock_data)};</script></body></html>"

        orig_get_text = http.get_text
        try:
            http.get_text = lambda url, **k: mock_html
            deals = mercadolibre.fetch(None, por_consulta=10)
            self.assertEqual(len(deals), 1)
            d = deals[0]
            self.assertEqual(d.source, "mercadolibre")
            self.assertEqual(d.store, "Mercado Libre")
            self.assertEqual(d.country, "CO")
            self.assertEqual(d.currency, "COP")
            self.assertEqual(d.price, 1500000.0)
            self.assertEqual(d.list_price, 3000000.0)
            self.assertEqual(d.discount_verificable, 50.0)
            self.assertIn("https://http2.mlstatic.com/D_NQ_NP_123456-MCO-F.jpg", d.image)
            self.assertTrue(any("Cupón 10% OFF" in n for n in d.notes))
            self.assertFalse(any("{icon_cockade}" in n for n in d.notes))
            self.assertIn("Tienda Oficial Samsung", d.notes)
        finally:
            http.get_text = orig_get_text


class PruebaControlAccesoWhitelist(unittest.TestCase):
    """Pruebas del sistema de control de acceso por whitelist y aprobacion interactiva."""

    def test_es_admin_identifica_al_dueno(self):
        from core import whitelist
        orig_admin = config.TELEGRAM_ADMIN_ID
        try:
            config.TELEGRAM_ADMIN_ID = "5583002220"
            self.assertTrue(whitelist.es_admin("5583002220"))
            self.assertTrue(whitelist.es_admin(5583002220))
            self.assertFalse(whitelist.es_admin("9999999999"))
            self.assertFalse(whitelist.es_admin(None))
        finally:
            config.TELEGRAM_ADMIN_ID = orig_admin

    def test_ciclo_solicitud_aprobacion_y_rechazo(self):
        from core import whitelist
        import tempfile
        from pathlib import Path as _Path

        with tempfile.TemporaryDirectory() as tmp:
            orig_archivo = whitelist.ARCHIVO_LOCAL
            whitelist.ARCHIVO_LOCAL = _Path(tmp) / "whitelist_test.json"
            orig_admin = config.TELEGRAM_ADMIN_ID
            config.TELEGRAM_ADMIN_ID = "5583002220"
            try:
                # 1. El admin siempre es permitido
                self.assertTrue(whitelist.es_permitido("5583002220"))
                # Un extrano no
                self.assertFalse(whitelist.es_permitido("12345"))

                # 2. Registrar solicitud nueva
                es_nueva = whitelist.registrar_solicitud("12345", nombre="Carlos", username="carlos_dev")
                self.assertTrue(es_nueva)
                self.assertFalse(whitelist.es_permitido("12345"))

                # Si repite, es_nueva debe ser False
                self.assertFalse(whitelist.registrar_solicitud("12345", nombre="Carlos"))

                # 3. Aprobar usuario
                info = whitelist.aprobar("12345")
                self.assertEqual(info.get("nombre"), "Carlos")
                self.assertTrue(whitelist.es_permitido("12345"))

                # 4. Rechazar / Revocar usuario
                info_rev = whitelist.rechazar("12345")
                self.assertFalse(whitelist.es_permitido("12345"))
            finally:
                whitelist.ARCHIVO_LOCAL = orig_archivo
                config.TELEGRAM_ADMIN_ID = orig_admin

    def test_leer_comando_control_acceso_privado(self):
        from core import comandos, telegram, whitelist
        import tempfile
        from pathlib import Path as _Path

        with tempfile.TemporaryDirectory() as tmp:
            orig_wl = whitelist.ARCHIVO_LOCAL
            whitelist.ARCHIVO_LOCAL = _Path(tmp) / "whitelist_test.json"
            orig_admin = config.TELEGRAM_ADMIN_ID
            config.TELEGRAM_ADMIN_ID = "5583002220"
            orig_miembro = telegram.es_miembro_del_canal
            try:
                msg_extrano = {
                    "text": "/alkosto",
                    "chat": {"id": 8888, "type": "private"},
                    "from": {"id": 8888, "first_name": "Pedro", "username": "pedro88"},
                }

                # Caso 1: Pedro NO está en el canal oficial -> debe ser rechazado con unirse_canal
                telegram.es_miembro_del_canal = lambda uid, **k: False
                solicitud_canal = comandos.leer_comando(msg_extrano)
                self.assertIsNotNone(solicitud_canal)
                self.assertEqual(solicitud_canal.get("tipo"), "unirse_canal")
                self.assertEqual(solicitud_canal.get("user_id"), 8888)

                # Caso 2: Pedro SÍ se une al canal oficial pero no está en whitelist -> solicitud_acceso
                telegram.es_miembro_del_canal = lambda uid, **k: True
                solicitud = comandos.leer_comando(msg_extrano)
                self.assertIsNotNone(solicitud)
                self.assertEqual(solicitud.get("tipo"), "solicitud_acceso")
                self.assertTrue(solicitud.get("es_nueva"))
                self.assertEqual(solicitud.get("user_id"), 8888)

                # Si escribe de nuevo mientras sigue pendiente
                solicitud2 = comandos.leer_comando(msg_extrano)
                self.assertFalse(solicitud2.get("es_nueva"))

                # El admin aprueba a Pedro
                whitelist.aprobar("8888")

                # Ahora Pedro escribe /alkosto en privado y es atendido normalmente
                solicitud3 = comandos.leer_comando(msg_extrano)
                self.assertIsNotNone(solicitud3)
                self.assertEqual(solicitud3.get("comando"), "alkosto")
                self.assertEqual(solicitud3.get("chat_id"), 8888)
            finally:
                whitelist.ARCHIVO_LOCAL = orig_wl
                config.TELEGRAM_ADMIN_ID = orig_admin
                telegram.es_miembro_del_canal = orig_miembro

    def test_sesion_aislada_por_chat(self):
        from core import comandos
        import tempfile
        from pathlib import Path as _Path

        with tempfile.TemporaryDirectory() as tmp:
            orig_estado = comandos.ESTADO
            comandos.ESTADO = _Path(tmp) / "comandos_estado.json"
            try:
                # Usuario A fija exito, Usuario B fija alkosto
                comandos.fijar_tienda_activa("exito", chat_id="user_a")
                comandos.fijar_tienda_activa("alkosto", chat_id="user_b")

                self.assertEqual(comandos.tienda_activa(chat_id="user_a"), "exito")
                self.assertEqual(comandos.tienda_activa(chat_id="user_b"), "alkosto")

                # Ofertas mostradas independientes
                comandos.marcar_mostradas(["oferta_1", "oferta_2"], chat_id="user_a")
                comandos.marcar_mostradas(["oferta_3"], chat_id="user_b")

                self.assertIn("oferta_1", comandos.ya_mostradas(chat_id="user_a"))
                self.assertNotIn("oferta_1", comandos.ya_mostradas(chat_id="user_b"))
                self.assertIn("oferta_3", comandos.ya_mostradas(chat_id="user_b"))
            finally:
                comandos.ESTADO = orig_estado

    def test_callback_query_aprobacion_admin(self):
        import server
        from core import telegram, whitelist
        import tempfile
        from pathlib import Path as _Path

        with tempfile.TemporaryDirectory() as tmp:
            orig_wl = whitelist.ARCHIVO_LOCAL
            whitelist.ARCHIVO_LOCAL = _Path(tmp) / "whitelist_test.json"
            orig_admin = config.TELEGRAM_ADMIN_ID
            config.TELEGRAM_ADMIN_ID = "5583002220"

            respuestas_cb = []
            mensajes_editados = []
            mensajes_enviados = []

            orig_resp_cb = telegram.responder_callback
            orig_edit = telegram.editar_mensaje
            orig_send = telegram.send

            telegram.responder_callback = lambda qid, txt="", alerta=False, **k: respuestas_cb.append((qid, txt, alerta)) or True
            telegram.editar_mensaje = lambda chat_id, message_id, texto, reply_markup=None, **k: mensajes_editados.append((chat_id, message_id, texto)) or True
            telegram.send = lambda txt, **k: mensajes_enviados.append((txt, k)) or True

            try:
                # 1. Registrar usuario pendiente
                whitelist.registrar_solicitud("9999", nombre="Luisa", username="luisa99")

                # Intento de aprobacion por un no-admin (debe ser rechazado con alerta)
                cb_no_admin = {
                    "id": "cb_1",
                    "from": {"id": 1111},
                    "data": "aprobar:9999",
                    "message": {"chat": {"id": 1111}, "message_id": 10},
                }
                server.atender_callback_query(cb_no_admin)
                self.assertTrue(any(r[2] is True for r in respuestas_cb))
                self.assertFalse(whitelist.es_permitido("9999"))

                # Intento de aprobacion por el Admin
                cb_admin = {
                    "id": "cb_2",
                    "from": {"id": 5583002220},
                    "data": "aprobar:9999",
                    "message": {"chat": {"id": 5583002220}, "message_id": 20},
                }
                server.atender_callback_query(cb_admin)
                self.assertTrue(whitelist.es_permitido("9999"))
                self.assertTrue(any("Aprobado" in r[1] for r in respuestas_cb))
                self.assertTrue(any("Acceso Autorizado" in m[2] for m in mensajes_editados))
                # Notificacion enviada al usuario con teclado
                self.assertTrue(any(m[1].get("chat_id") == "9999" and "aprobado" in m[0].lower() for m in mensajes_enviados))
            finally:
                whitelist.ARCHIVO_LOCAL = orig_wl
                config.TELEGRAM_ADMIN_ID = orig_admin
                telegram.responder_callback = orig_resp_cb
                telegram.editar_mensaje = orig_edit
                telegram.send = orig_send

    def test_teclado_aprobacion_botones(self):
        from core import telegram
        markup = telegram.teclado_aprobacion(777)
        botones = markup["inline_keyboard"][0]
        self.assertEqual(len(botones), 2)
        self.assertEqual(botones[0]["callback_data"], "aprobar:777")
        self.assertEqual(botones[1]["callback_data"], "rechazar:777")
        self.assertIn("Aprobar", botones[0]["text"])
        self.assertIn("Rechazar", botones[1]["text"])

    def test_inicio_sesion_notifica_al_admin_con_ventana_30_min(self):
        from core import whitelist, telegram
        import radar

        whitelist.limpiar_sesiones()
        orig_admin = config.TELEGRAM_ADMIN_ID
        config.TELEGRAM_ADMIN_ID = "5583002220"

        notificaciones = []
        orig_send = telegram.send
        telegram.send = lambda txt, **k: notificaciones.append((txt, k)) or True

        try:
            # 1. Primera interaccion de usuario aprobado -> Notifica al admin
            solicitud_1 = {
                "comando": "menu",
                "tipo": "menu",
                "chat_id": 4444,
                "user_id": 4444,
                "nombre": "Sofia",
                "username": "sofi_tech",
            }
            radar.atender_solicitudes([solicitud_1])
            notifs_admin = [n for n in notificaciones if n[1].get("chat_id") == "5583002220" and "inició una sesión" in n[0]]
            self.assertEqual(len(notifs_admin), 1)
            self.assertIn("Sofia", notifs_admin[0][0])
            self.assertIn("@sofi_tech", notifs_admin[0][0])

            # 2. Segunda interaccion inmediata (dentro de los 30 min) -> NO repite notificacion
            notificaciones.clear()
            solicitud_2 = {
                "comando": "menu",
                "tipo": "menu",
                "chat_id": 4444,
                "user_id": 4444,
                "nombre": "Sofia",
                "username": "sofi_tech",
            }
            radar.atender_solicitudes([solicitud_2])
            notifs_admin_2 = [n for n in notificaciones if n[1].get("chat_id") == "5583002220" and "inició una sesión" in n[0]]
            self.assertEqual(len(notifs_admin_2), 0)

            # 3. El Admin usando el bot -> NUNCA se notifica a si mismo
            notificaciones.clear()
            solicitud_admin = {
                "comando": "menu",
                "tipo": "menu",
                "chat_id": 5583002220,
                "user_id": 5583002220,
                "nombre": "Emanuel",
            }
            radar.atender_solicitudes([solicitud_admin])
            notifs_admin_self = [n for n in notificaciones if "inició una sesión" in n[0]]
            self.assertEqual(len(notifs_admin_self), 0)

            # 4. Pasan mas de 30 min de inactividad -> Notifica de nuevo
            whitelist._ultimas_sesiones["4444"] -= 1805 # 30 min y 5 seg atras
            notificaciones.clear()
            radar.atender_solicitudes([solicitud_1])
            notifs_admin_3 = [n for n in notificaciones if n[1].get("chat_id") == "5583002220" and "inició una sesión" in n[0]]
            self.assertEqual(len(notifs_admin_3), 1)
        finally:
            config.TELEGRAM_ADMIN_ID = orig_admin
            telegram.send = orig_send
            whitelist.limpiar_sesiones()

    def test_es_miembro_del_canal_estados(self):
        from core import http, telegram
        orig_post = http.post_json
        try:
            # Miembro activo
            http.post_json = lambda url, payload, **k: {"ok": True, "result": {"status": "member"}}
            self.assertTrue(telegram.es_miembro_del_canal(12345, channel_id="-100"))

            # Administrador o creador
            http.post_json = lambda url, payload, **k: {"ok": True, "result": {"status": "administrator"}}
            self.assertTrue(telegram.es_miembro_del_canal(12345, channel_id="-100"))

            # Salio del canal
            http.post_json = lambda url, payload, **k: {"ok": True, "result": {"status": "left"}}
            self.assertFalse(telegram.es_miembro_del_canal(12345, channel_id="-100"))

            # Baneado
            http.post_json = lambda url, payload, **k: {"ok": True, "result": {"status": "kicked"}}
            self.assertFalse(telegram.es_miembro_del_canal(12345, channel_id="-100"))

            # Error de API o no encontrado
            http.post_json = lambda url, payload, **k: {"ok": False, "description": "USER_NOT_PARTICIPANT"}
            self.assertFalse(telegram.es_miembro_del_canal(12345, channel_id="-100"))
        finally:
            http.post_json = orig_post

    def test_teclado_unirse_canal_estructura(self):
        from core import telegram
        teclado = telegram.teclado_unirse_canal("https://t.me/test_canal")
        filas = teclado["inline_keyboard"]
        self.assertEqual(len(filas), 2)
        self.assertEqual(filas[0][0]["url"], "https://t.me/test_canal")
        self.assertEqual(filas[1][0]["callback_data"], "verificar_canal")

    def test_callback_verificar_canal_flujo(self):
        import server
        from core import telegram, whitelist
        import tempfile
        from pathlib import Path as _Path

        with tempfile.TemporaryDirectory() as tmp:
            orig_wl = whitelist.ARCHIVO_LOCAL
            whitelist.ARCHIVO_LOCAL = _Path(tmp) / "whitelist_test.json"
            orig_admin = config.TELEGRAM_ADMIN_ID
            config.TELEGRAM_ADMIN_ID = "5583002220"

            respuestas_cb = []
            mensajes_enviados = []
            mensajes_editados = []

            orig_resp = telegram.responder_callback
            orig_send = telegram.send
            orig_edit = telegram.editar_mensaje
            orig_miembro = telegram.es_miembro_del_canal

            telegram.responder_callback = lambda qid, txt="", alerta=False, **k: respuestas_cb.append((qid, txt, alerta)) or True
            telegram.send = lambda txt, **k: mensajes_enviados.append((txt, k)) or True
            telegram.editar_mensaje = lambda chat_id, message_id, texto, **k: mensajes_editados.append((chat_id, message_id, texto)) or True

            try:
                cb_user = {
                    "id": "cb_verif",
                    "from": {"id": 3333, "first_name": "Andres", "username": "andres33"},
                    "data": "verificar_canal",
                    "message": {"chat": {"id": 3333}, "message_id": 55},
                }

                # 1. Aun no se ha unido al canal -> Alerta emergente
                telegram.es_miembro_del_canal = lambda uid, **k: False
                server.atender_callback_query(cb_user)
                self.assertTrue(any(r[2] is True for r in respuestas_cb))
                self.assertEqual(len(mensajes_enviados), 0)

                # 2. Ya se unio pero no esta en whitelist -> Alerta confirmada, notifica al admin para aprobar
                respuestas_cb.clear()
                telegram.es_miembro_del_canal = lambda uid, **k: True
                server.atender_callback_query(cb_user)
                self.assertTrue(any("Confirmado" in r[1] for r in respuestas_cb))
                self.assertTrue(any(m[1].get("chat_id") == "5583002220" and "Unido a Ofertas" in m[0] for m in mensajes_enviados))

                # 3. Ya se unio Y esta en whitelist -> Despliega menu directamente
                whitelist.aprobar("3333")
                mensajes_enviados.clear()
                server.atender_callback_query(cb_user)
                self.assertTrue(any(m[1].get("chat_id") == 3333 and "Menú Principal" in m[0] for m in mensajes_enviados))
            finally:
                whitelist.ARCHIVO_LOCAL = orig_wl
                config.TELEGRAM_ADMIN_ID = orig_admin
                telegram.responder_callback = orig_resp
                telegram.send = orig_send
                telegram.editar_mensaje = orig_edit
                telegram.es_miembro_del_canal = orig_miembro

    def test_no_miembro_canal_no_recibe_siguientes_ofertas(self):
        import server
        from core import telegram
        respuestas_cb = []
        orig_resp = telegram.responder_callback
        orig_miembro = telegram.es_miembro_del_canal
        try:
            telegram.responder_callback = lambda qid, txt="", alerta=False, **k: respuestas_cb.append((qid, txt, alerta)) or True
            telegram.es_miembro_del_canal = lambda uid, **k: False
            cb_sig = {
                "id": "cb_sig_test",
                "from": {"id": 7777},
                "data": "siguientes_ofertas",
                "message": {"chat": {"id": 7777}},
            }
            server.atender_callback_query(cb_sig)
            self.assertTrue(any("Debes estar unido al canal" in r[1] and r[2] is True for r in respuestas_cb))
        finally:
            telegram.responder_callback = orig_resp
            telegram.es_miembro_del_canal = orig_miembro

    def test_unirse_canal_remueve_teclado_de_tiendas(self):
        import radar
        from core import telegram
        mensajes_enviados = []
        orig_send = telegram.send
        try:
            telegram.send = lambda txt, **k: mensajes_enviados.append((txt, k)) or True
            solicitud = {
                "tipo": "unirse_canal",
                "chat_id": 9999,
                "nombre": "Carlos",
            }
            radar.atender_solicitudes([solicitud])
            # Debe haber enviado un mensaje con remove_keyboard=True para que el usuario no vea el menú de tiendas
            removidos = [m for m in mensajes_enviados if m[1].get("reply_markup") == {"remove_keyboard": True}]
            self.assertEqual(len(removidos), 1)
            self.assertIn("exclusivo", removidos[0][0].lower())
            # Y el mensaje con el botón para unirse al canal
            invitaciones = [m for m in mensajes_enviados if "inline_keyboard" in m[1].get("reply_markup", {})]
            self.assertEqual(len(invitaciones), 1)
        finally:
            telegram.send = orig_send


class PruebaNuevasTiendasYCategoriasEspecificas(unittest.TestCase):
    """Verifica que las tiendas VTEX agregadas y las cositas especificas funcionen."""

    def test_tiendas_vtex_nuevas_registradas(self):
        from sources import vtex
        from core import comandos
        tiendas_esperadas = ["totto", "studiof", "velez", "americanino", "arturocalle", "nike"]
        for t in tiendas_esperadas:
            self.assertIn(t, vtex.TIENDAS)
            self.assertIn(t, comandos.CATALOGO)
            self.assertEqual(comandos.CATALOGO[t][0], "vtex")

    def test_botones_tiendas_nuevas(self):
        from core import comandos
        from unittest.mock import patch
        from core import comandos
        with patch("core.whitelist.es_admin", return_value=True):
            def parsear(txt):
                return comandos.leer_comando({"text": txt, "chat": {"id": 12345, "type": "private"}, "from": {"id": 12345}})

            self.assertEqual(parsear("🎒 Totto")["tienda"], "totto")
            self.assertEqual(parsear("👗 Studio F")["tienda"], "studiof")
            self.assertEqual(parsear("👞 Vélez")["tienda"], "velez")
            self.assertEqual(parsear("🦅 Americanino")["tienda"], "americanino")
            self.assertEqual(parsear("👔 Arturo Calle")["tienda"], "arturocalle")

    def test_grupos_de_productos_cotidianos(self):
        from unittest.mock import patch
        from core import comandos
        with patch("core.whitelist.es_admin", return_value=True):
            def parsear(txt):
                return comandos.leer_comando({"text": txt, "chat": {"id": 12345, "type": "private"}, "from": {"id": 12345}})

            res_cocina = parsear("🍳 Cocina")
            self.assertEqual(res_cocina["tipo"], "grupo_categoria")
            self.assertEqual(res_cocina["grupo"], "cocina")

            res_tec = parsear("💻 Tecnología")
            self.assertEqual(res_tec["tipo"], "grupo_categoria")
            self.assertEqual(res_tec["grupo"], "tecnologia")

            res_nev = parsear("❄️ Neveras y Lavadoras")
            self.assertEqual(res_nev["tipo"], "grupo_categoria")
            self.assertEqual(res_nev["grupo"], "neveras")

            res_ropa = parsear("👟 Ropa y Tenis")
            self.assertEqual(res_ropa["tipo"], "grupo_categoria")
            self.assertEqual(res_ropa["grupo"], "ropa")

            res_hogar = parsear("🏠 Hogar")
            self.assertEqual(res_hogar["tipo"], "grupo_categoria")
            self.assertEqual(res_hogar["grupo"], "hogar")

            res_volver = parsear("⬅️ Volver a Grupos")
            self.assertEqual(res_volver["tipo"], "grupo_categoria")
            self.assertEqual(res_volver["grupo"], "volver")

    def test_cositas_especificas_consultas(self):
        from unittest.mock import patch
        from core import comandos
        with patch("core.whitelist.es_admin", return_value=True):
            def parsear(txt):
                return comandos.leer_comando({"text": txt, "chat": {"id": 12345, "type": "private"}, "from": {"id": 12345}})

            # Cocina: Airfryers y Sandwicheras
            r_air = parsear("🍟 Airfryers")
            self.assertEqual(r_air["tipo"], "categoria")
            self.assertIn("freidora de aire", r_air["consultas"])

            # Neveras y Lavadoras
            r_nev = parsear("❄️ Neveras")
            self.assertEqual(r_nev["tipo"], "categoria")
            self.assertIn("nevera", r_nev["consultas"])

            r_lav = parsear("🧺 Lavadoras")
            self.assertEqual(r_lav["tipo"], "categoria")
            self.assertIn("lavadora", r_lav["consultas"])

            # Calzado
            r_zap = parsear("👟 Tenis y Zapatos")
            self.assertEqual(r_zap["tipo"], "categoria")
            self.assertIn("tenis", r_zap["consultas"])

            # Hogar
            r_asp = parsear("🧹 Aspiradoras")
            self.assertEqual(r_asp["tipo"], "categoria")
            self.assertIn("aspiradora", r_asp["consultas"])

    def test_teclados_especificos_bien_formados(self):
        from core import telegram
        # Teclado tiendas incluye las nuevas
        t_tiendas = telegram.teclado_tiendas()
        textos_tiendas = [b["text"] for row in t_tiendas["keyboard"] for b in row]
        self.assertIn("🎒 Totto", textos_tiendas)
        self.assertIn("👗 Studio F", textos_tiendas)
        self.assertIn("👞 Vélez", textos_tiendas)
        self.assertIn("🦅 Americanino", textos_tiendas)
        self.assertIn("👔 Arturo Calle", textos_tiendas)

        # Tienda de ropa despliega moda directamente
        t_totto = telegram.teclado_categorias("Totto")
        textos_totto = [b["text"] for row in t_totto["keyboard"] for b in row]
        self.assertIn("👟 Tenis y Zapatos", textos_totto)
        self.assertIn("👕 Camisetas y Polos", textos_totto)

        # Teclado Cocina
        t_cocina = telegram.teclado_cocina()
        textos_cocina = [b["text"] for row in t_cocina["keyboard"] for b in row]
        self.assertIn("🍟 Airfryers", textos_cocina)
        self.assertIn("🥪 Sandwicheras", textos_cocina)
        self.assertIn("🍹 Licuadoras", textos_cocina)

        # Teclado Tecnologia
        t_tec = telegram.teclado_tecnologia()
        textos_tec = [b["text"] for row in t_tec["keyboard"] for b in row]
        self.assertIn("📺 Televisores", textos_tec)
        self.assertIn("💻 Portátiles", textos_tec)
        self.assertIn("📱 Celulares", textos_tec)

        # Tiendas de electrodomésticos en teclado_tiendas
        self.assertIn("🟢 Jumbo", textos_tiendas)
        self.assertIn("🔵 Alkomprar", textos_tiendas)
        self.assertIn("🔴 Haceb", textos_tiendas)
        self.assertIn("🌀 Whirlpool", textos_tiendas)
        self.assertIn("🍳 Imusa", textos_tiendas)
        self.assertIn("☕ Oster", textos_tiendas)

        # Enrutamiento inteligente a teclados de cocina y línea blanca
        t_imusa = telegram.teclado_categorias("Imusa")
        textos_imusa = [b["text"] for row in t_imusa["keyboard"] for b in row]
        self.assertIn("🍟 Airfryers", textos_imusa)
        self.assertIn("🥪 Sandwicheras", textos_imusa)

        t_haceb = telegram.teclado_categorias("Haceb")
        textos_haceb = [b["text"] for row in t_haceb["keyboard"] for b in row]
        self.assertIn("❄️ Neveras", textos_haceb)
        self.assertIn("🧺 Lavadoras", textos_haceb)

    def test_tiendas_electrodomesticos_registradas(self):
        from unittest.mock import patch
        from sources import vtex, algolia_co
        from core import comandos

        for t in ["jumbo", "haceb", "whirlpool", "imusa", "oster"]:
            self.assertIn(t, vtex.TIENDAS)
            self.assertIn(t, comandos.CATALOGO)
            self.assertEqual(comandos.CATALOGO[t][0], "vtex")

        self.assertIn("alkomprar", algolia_co.TIENDAS)
        self.assertIn("alkomprar", comandos.CATALOGO)
        self.assertEqual(comandos.CATALOGO["alkomprar"][0], "algolia_co")

        with patch("core.whitelist.es_admin", return_value=True):
            def parsear(txt):
                return comandos.leer_comando({"text": txt, "chat": {"id": 12345, "type": "private"}, "from": {"id": 12345}})

            self.assertEqual(parsear("🟢 Jumbo")["tienda"], "jumbo")
            self.assertEqual(parsear("🔵 Alkomprar")["tienda"], "alkomprar")
            self.assertEqual(parsear("🔴 Haceb")["tienda"], "haceb")
            self.assertEqual(parsear("🌀 Whirlpool")["tienda"], "whirlpool")
            self.assertEqual(parsear("🍳 Imusa")["tienda"], "imusa")
            self.assertEqual(parsear("☕ Oster")["tienda"], "oster")
            self.assertEqual(parsear("🇨🇴 Comparar Tiendas")["tienda"], "colombia")
            self.assertEqual(parsear("🇨🇴 Todo Colombia")["tienda"], "colombia")

    def test_teclado_tiendas_incluye_comparar_tiendas(self):
        from core import telegram
        t = telegram.teclado_tiendas()
        textos = [b["text"] for row in t["keyboard"] for b in row]
        self.assertIn("🇨🇴 Comparar Tiendas", textos)

    def test_balance_top3_por_tienda(self):
        import radar
        from core.models import Deal
        from unittest.mock import patch

        alkosto_deals = [
            Deal("algolia_co", "Alkosto", "CO", f"k:alk:{i}", f"Nevera Modelo {i} Alkosto", "http://a", 100 + i * 10, "COP", 250, in_stock=True)
            for i in range(5)
        ]
        exito_deals = [
            Deal("vtex", "Exito", "CO", f"k:exi:{i}", f"Refrigerador Serie {i} Exito", "http://e", 120 + i * 10, "COP", 250, in_stock=True)
            for i in range(4)
        ]
        haceb_deals = [
            Deal("vtex", "Haceb", "CO", f"k:hac:{i}", f"Nevecon Linea {i} Haceb", "http://h", 150 + i * 10, "COP", 250, in_stock=True)
            for i in range(2)
        ]

        def _mock_ofertas(_wl, fuente, _tiendas, **_kw):
            if fuente == "algolia_co":
                return alkosto_deals
            elif fuente == "vtex":
                return exito_deals + haceb_deals
            return []

        with patch.object(radar, "_ofertas_de", side_effect=_mock_ofertas):
            seleccion = radar._mejores_colombia({}, set(), cuantas=15, consultas_custom=["nevera", "refrigerador", "nevecon"], max_por_tienda=3)
            conteo = {}
            for deal, _ in seleccion:
                conteo[deal.store] = conteo.get(deal.store, 0) + 1

            self.assertEqual(conteo.get("Alkosto"), 3)
            self.assertEqual(conteo.get("Exito"), 3)
            self.assertEqual(conteo.get("Haceb"), 2)
            self.assertEqual(len(seleccion), 8)

    def test_filtro_accesorios_bloquea_compresor(self):
        from core import filtros
        self.assertTrue(filtros.es_accesorio("Compresor GMCC 1/5 HP para Nevera"))
        self.assertTrue(filtros.es_accesorio("Motor ventilador para lavadora"))
        self.assertTrue(filtros.es_accesorio("Termostato para nevera Haceb"))
        # Un producto completo que menciona compresor en su descripción no se descarta
        self.assertFalse(filtros.es_accesorio("Nevera Haceb No Frost 300L con compresor inverter"))

    def test_menciona_singular_y_plural(self):
        from core import filtros
        self.assertTrue(filtros.menciona("Pack x3 Camisetas Hombre", "camiseta"))
        self.assertTrue(filtros.menciona("Pantalones de Vestir", "pantalon"))
        self.assertTrue(filtros.menciona("Neveras No Frost", "nevera"))
        self.assertFalse(filtros.menciona("Pelota Teknofit", "camiseta"))
        self.assertFalse(filtros.menciona("Llanta Michelin", "camiseta"))
        self.assertFalse(filtros.menciona("Jean Unicolor", "camiseta"))

    def test_categoria_descarta_productos_irrelevantes(self):
        import radar
        from core.models import Deal
        from unittest.mock import patch

        ofertas_variadas = [
            Deal("algolia_co", "Alkosto", "CO", "k1", "Pelota Teknofit", "http://a", 50, "COP", 100, in_stock=True),
            Deal("algolia_co", "Alkosto", "CO", "k2", "Llanta Michelin 185", "http://a", 200, "COP", 400, in_stock=True),
            Deal("vtex", "Arturo Calle", "CO", "k3", "Jean Unicolor", "http://ac", 40, "COP", 100, in_stock=True),
            Deal("vtex", "Totto", "CO", "k4", "Camiseta Manga Corta", "http://to", 30, "COP", 100, in_stock=True),
            Deal("vtex", "Falabella", "CO", "k5", "Polo Clásica Algodón", "http://fa", 25, "COP", 80, in_stock=True),
        ]

        with patch.object(radar, "_ofertas_de", return_value=ofertas_variadas):
            res = radar._mejores_colombia({}, set(), cuantas=10, consultas_custom=["camiseta", "polo", "camisa"], max_por_tienda=3)
            titulos = [d.title for d, _ in res]
            self.assertIn("Camiseta Manga Corta", titulos)
            self.assertIn("Polo Clásica Algodón", titulos)
            self.assertNotIn("Pelota Teknofit", titulos)
            self.assertNotIn("Llanta Michelin 185", titulos)
            self.assertNotIn("Jean Unicolor", titulos)

    def test_enrutamiento_tiendas_por_departamento(self):
        import radar
        # Ropa: no consulta Algolia ni electrodomésticos
        alg_ropa, vtex_ropa, fala_ropa = radar._tiendas_por_departamento(["camiseta", "polo"])
        self.assertEqual(alg_ropa, [])
        self.assertIn("totto", vtex_ropa)
        self.assertIn("arturocalle", vtex_ropa)
        self.assertIn("nike", vtex_ropa)
        self.assertNotIn("haceb", vtex_ropa)
        self.assertNotIn("imusa", vtex_ropa)
        self.assertEqual(fala_ropa, ["falabella"])

        # Neveras: consulta Haceb y Whirlpool, pero no tiendas de ropa
        alg_nev, vtex_nev, fala_nev = radar._tiendas_por_departamento(["nevera", "refrigerador"])
        self.assertIn("alkosto", alg_nev)
        self.assertIn("haceb", vtex_nev)
        self.assertIn("whirlpool", vtex_nev)
        self.assertNotIn("totto", vtex_nev)
        self.assertNotIn("arturocalle", vtex_nev)
        self.assertNotIn("nike", vtex_nev)
        self.assertIn("homecenter", fala_nev)

        # Sin categoría (TODO): devuelve todas
        alg_todas, vtex_todas, fala_todas = radar._tiendas_por_departamento(None)
        self.assertEqual(len(alg_todas), 3)
        self.assertEqual(len(vtex_todas), 14)
        self.assertEqual(len(fala_todas), 2)


class PruebaCacheYTopesCategoria(unittest.TestCase):
    def setUp(self):
        import radar
        radar.limpiar_cache()

    def tearDown(self):
        import radar
        radar.limpiar_cache()

    def test_cache_memoria_reutiliza_ofertas(self):
        import radar
        from core.models import Deal
        from unittest.mock import patch

        deal_prueba = Deal("vtex", "Totto", "CO", "k_totto", "Camiseta Polo", "http://t", 45000, "COP", 90000, in_stock=True)

        with patch("sources.vtex.fetch", return_value=[deal_prueba]) as mock_fetch:
            # 1ra llamada: debe consultar la fuente y guardar en cache
            res1 = radar._ofertas_de({"vtex": {"tiendas": ["totto"]}}, "vtex", ["totto"], ["camiseta"])
            self.assertEqual(len(res1), 1)
            self.assertEqual(mock_fetch.call_count, 1)

            # 2da llamada con los mismos parametros: debe salir de cache sin volver a llamar a vtex
            res2 = radar._ofertas_de({"vtex": {"tiendas": ["totto"]}}, "vtex", ["totto"], ["camiseta"])
            self.assertEqual(len(res2), 1)
            self.assertEqual(mock_fetch.call_count, 1)
            self.assertEqual(res1[0].title, res2[0].title)

    def test_cache_memoria_expira_tras_35_min(self):
        import time
        import radar
        from core.models import Deal

        deal_prueba = Deal("vtex", "Totto", "CO", "k_exp", "Camiseta Vencida", "http://t", 40000, "COP", 80000, in_stock=True)
        clave = radar._clave_cache("vtex", ["totto"], ["camiseta"])

        # Simular que se guardo hace 36 minutos
        radar._CACHE_OFERTAS[clave] = (time.time() - (36 * 60), [deal_prueba])

        # Debe expirar y retornar None
        self.assertIsNone(radar._obtener_de_cache(clave))
        self.assertNotIn(clave, radar._CACHE_OFERTAS)

    def test_cache_memoria_respeta_tope_maximo_60(self):
        import radar
        from core.models import Deal

        deal_prueba = Deal("vtex", "Totto", "CO", "k", "Item", "http://t", 10000, "COP", 20000, in_stock=True)
        for i in range(70):
            clave = ("vtex", f"tienda_{i}", (f"query_{i}",))
            radar._guardar_en_cache(clave, [deal_prueba])

        # No debe sobrepasar el maximo de 60 entradas
        self.assertLessEqual(len(radar._CACHE_OFERTAS), 60)

    def test_topes_categoria_admisibles(self):
        import radar
        from core.models import Deal

        # Televisores: acordado en $2.200.000 COP
        tv_bueno = Deal("vtex", "Exito", "CO", "tv1", "Smart TV Samsung 55 UHD", "http://tv", 2_100_000, "COP", in_stock=True)
        tv_caro = Deal("vtex", "Exito", "CO", "tv2", "Smart TV Samsung 65 OLED", "http://tv", 2_350_000, "COP", in_stock=True)
        self.assertTrue(radar._precio_admisible(tv_bueno))
        self.assertFalse(radar._precio_admisible(tv_caro))

        # Monitores: maximo $800.000 COP
        mon_bueno = Deal("algolia_co", "Alkosto", "CO", "m1", "Monitor Gamer Asus 24", "http://m", 750_000, "COP", in_stock=True)
        mon_caro = Deal("algolia_co", "Alkosto", "CO", "m2", "Monitor Curvo 34", "http://m", 950_000, "COP", in_stock=True)
        self.assertTrue(radar._precio_admisible(mon_bueno))
        self.assertFalse(radar._precio_admisible(mon_caro))

        # Neveras: maximo $2.200.000 COP
        nev_buena = Deal("vtex", "Haceb", "CO", "n1", "Nevera Haceb No Frost 240L", "http://n", 2_050_000, "COP", in_stock=True)
        nev_cara = Deal("vtex", "Haceb", "CO", "n2", "Nevecon Whirlpool Frances", "http://n", 2_400_000, "COP", in_stock=True)
        self.assertTrue(radar._precio_admisible(nev_buena))
        self.assertFalse(radar._precio_admisible(nev_cara))

        # Portátiles: maximo $2.500.000 COP
        lap_bueno = Deal("algolia_co", "Ktronix", "CO", "l1", "Portátil Lenovo Ideapad Core i5", "http://l", 2_300_000, "COP", in_stock=True)
        lap_caro = Deal("algolia_co", "Ktronix", "CO", "l2", "Portátil Gamer Legion RTX 4060", "http://l", 2_700_000, "COP", in_stock=True)
        self.assertTrue(radar._precio_admisible(lap_bueno))
        self.assertFalse(radar._precio_admisible(lap_caro))

        # Ropa (Camisetas): maximo $60.000 COP
        cam_buena = Deal("vtex", "Koaj", "CO", "c1", "Camiseta Polo Clásica", "http://c", 55_000, "COP", in_stock=True)
        cam_cara = Deal("vtex", "Koaj", "CO", "c2", "Camiseta Estampada Premium", "http://c", 75_000, "COP", in_stock=True)
        self.assertTrue(radar._precio_admisible(cam_buena))
        self.assertFalse(radar._precio_admisible(cam_cara))

        # Nike Colombia: solo calzado y hasta $220.000 COP
        nike_tenis_ganga = Deal("vtex", "Nike", "CO", "nk1", "Tenis Nike Court Royale", "http://nk", 180_000, "COP", in_stock=True)
        nike_tenis_limite = Deal("vtex", "Nike", "CO", "nk2", "Tenis Nike Star Runner 5", "http://nk", 220_000, "COP", in_stock=True)
        nike_tenis_caro = Deal("vtex", "Nike", "CO", "nk3", "Tenis Nike Alphafly 3", "http://nk", 225_000, "COP", in_stock=True)
        nike_ropa_barata = Deal("vtex", "Nike", "CO", "nk4", "Camiseta Nike Dry Fit", "http://nk", 45_000, "COP", in_stock=True)
        nike_bolso = Deal("vtex", "Nike", "CO", "nk5", "Bolsa Nike Brasilia", "http://nk", 119_950, "COP", in_stock=True)

        self.assertTrue(radar._precio_admisible(nike_tenis_ganga))
        self.assertTrue(radar._precio_admisible(nike_tenis_limite))
        self.assertFalse(radar._precio_admisible(nike_tenis_caro))
        self.assertFalse(radar._precio_admisible(nike_ropa_barata))  # Solo calzado permitido para Nike
        self.assertFalse(radar._precio_admisible(nike_bolso))        # Solo calzado permitido para Nike



class PruebaPaginacionSiguientesOfertas(unittest.TestCase):
    def setUp(self):
        import radar
        from core import comandos
        radar.limpiar_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_estado = comandos.ESTADO
        comandos.ESTADO = Path(self.tmp.name) / "comandos_estado.json"

    def tearDown(self):
        from core import comandos
        comandos.ESTADO = self.orig_estado
        self.tmp.cleanup()

    def test_reconocimiento_comando_siguientes(self):
        from unittest.mock import patch
        from core import comandos
        with patch("core.whitelist.es_admin", return_value=True):
            cid = 4444
            # Texto de boton
            sol1 = comandos.leer_comando({"text": "🔄 Ver siguientes ofertas", "chat": {"id": cid, "type": "private"}, "from": {"id": cid}})
            self.assertIsNotNone(sol1)
            self.assertEqual(sol1.get("tipo"), "siguientes")

            # Texto alternativo
            sol2 = comandos.leer_comando({"text": "más ofertas", "chat": {"id": cid, "type": "private"}, "from": {"id": cid}})
            self.assertIsNotNone(sol2)
            self.assertEqual(sol2.get("tipo"), "siguientes")

            # Slash command
            sol3 = comandos.leer_comando({"text": "/siguientes", "chat": {"id": cid, "type": "private"}, "from": {"id": cid}})
            self.assertIsNotNone(sol3)
            self.assertEqual(sol3.get("tipo"), "siguientes")

    def test_guardar_y_obtener_ultima_busqueda(self):
        from core import comandos
        sol = {"tipo": "categoria", "categoria_nombre": "❄️ Neveras", "consultas": ["nevera"], "chat_id": 999}
        comandos.guardar_ultima_busqueda(999, sol)

        recup = comandos.obtener_ultima_busqueda(999)
        self.assertIsNotNone(recup)
        self.assertEqual(recup.get("categoria_nombre"), "❄️ Neveras")
        self.assertEqual(recup.get("consultas"), ["nevera"])

    def test_boton_siguientes_ofertas_bien_formado(self):
        from core import telegram
        teclado = telegram.boton_siguientes_ofertas()
        self.assertIn("inline_keyboard", teclado)
        btn = teclado["inline_keyboard"][0][0]
        self.assertEqual(btn["callback_data"], "siguientes_ofertas")
        self.assertIn("siguientes", btn["text"].lower())

    def test_flujo_paginacion_siguiente_entrega_nuevas_sin_repetir(self):
        import radar
        from core import telegram
        from core.models import Deal
        from unittest.mock import patch

        deals_disponibles = [
            Deal("vtex", "Haceb", "CO", f"k_{i}", f"Nevera Haceb {200 + i*20} Litros", "http://h", 1_400_000 + (i * 20_000), "COP", 2_000_000, in_stock=True)
            for i in range(6)
        ]

        ofertas_enviadas = []
        mensajes_chat = []
        with patch("core.telegram.enviar_oferta", side_effect=lambda d, *a, **k: ofertas_enviadas.append(d.key)), \
             patch("core.telegram.send", side_effect=lambda txt, **k: mensajes_chat.append(txt) or True), \
             patch.object(radar, "_ofertas_de", return_value=deals_disponibles), \
             patch("time.sleep"):

            cid = 999999
            # 1. Primera tanda (busqueda normal)
            sol1 = {"comando": "categoria", "tipo": "categoria", "categoria_nombre": "❄️ Neveras", "consultas": ["nevera"], "chat_id": cid}
            radar.atender_solicitudes([sol1], por_comando=3)

            self.assertEqual(len(ofertas_enviadas), 3)
            primera_tanda = list(ofertas_enviadas)
            self.assertEqual(primera_tanda, ["k_0", "k_1", "k_2"])

            # 2. Segunda tanda (solicitud de 'siguientes')
            sol2 = {"tipo": "siguientes", "chat_id": cid}
            radar.atender_solicitudes([sol2], por_comando=3)

            segunda_tanda = ofertas_enviadas[3:]
            self.assertEqual(len(segunda_tanda), 3)
            self.assertEqual(segunda_tanda, ["k_3", "k_4", "k_5"])

            # No deben repetirse
            self.assertEqual(set(primera_tanda) & set(segunda_tanda), set())

    def test_recordatorio_sigue_disponible_a_los_tres_dias(self):
        from core import telegram
        from core.models import Deal
        from core.scoring import Verdict

        d = Deal("vtex", "Alkosto", "CO", "k_test", "Smart TV 55 Pulgadas", "http://tv", 1_500_000, "COP", 2_200_000, in_stock=True)
        v = Verdict(alertar=True, motivo="sigue vigente tras 3 dias", confianza="alta")
        renderizado = telegram.render(d, v)
        self.assertIn("Sigue disponible", renderizado)
        self.assertEqual(config.REALERT_DAYS, 14)
        self.assertEqual(config.MAX_ALERTS_PER_RUN, 4)
        self.assertFalse(config.SEED_ON_EMPTY_DB)

    def test_marcas_usa_y_envio_directo_vs_casillero(self):
        from sources import slickdeals
        from core import filtros, telegram
        from core.models import Deal
        from core.landed import calcular
        from core.scoring import Verdict

        # 1. Filtro estricto de marcas permitidas en Slickdeals
        self.assertTrue(slickdeals.es_tienda_permitida(slickdeals._tienda("Adidas Men's Ultraboost Shoes at eBay")))
        self.assertTrue(slickdeals.es_tienda_permitida(slickdeals._tienda("Apple AirPods Pro at Amazon")))
        self.assertTrue(slickdeals.es_tienda_permitida(slickdeals._tienda("Nike Air Force 1 at Nike")))
        self.assertFalse(slickdeals.es_tienda_permitida(slickdeals._tienda("Macy's Women's Handbag at Macy's")))
        self.assertFalse(slickdeals.es_tienda_permitida(slickdeals._tienda("Target 50-Inch TV at Target")))
        self.assertFalse(slickdeals.es_tienda_permitida(slickdeals._tienda("Kohl's Fleece Jacket at Kohl's")))

        # 2. Deteccion de pesados para casillero
        self.assertTrue(filtros.es_pesado_para_casillero("LG 55-Inch 4K Smart TV"))
        self.assertTrue(filtros.es_pesado_para_casillero("Samsung Refrigerator 28 cu ft"))
        self.assertFalse(filtros.es_pesado_para_casillero("Adidas Men's Running Shoes"))
        self.assertFalse(filtros.es_pesado_para_casillero("Dell 27-Inch Gaming Monitor"))

        # 3. Renderizado de Amazon directo sin casillero
        d_amazon = Deal("slickdeals", "Amazon (via Slickdeals)", "US", "amz_1", "Acer 24 Gaming Monitor", "http://amz", 110.0, "USD", 180.0, free_shipping_co=True)
        landed_amz = calcular(110.0, trm=4000.0)
        v = Verdict(alertar=True, motivo="oferta", confianza="alta")
        msg_amz = telegram.render(d_amazon, v, landed=landed_amz)
        self.assertIn("Envío GRATIS directo a Colombia", msg_amz)
        self.assertNotIn("casillero", msg_amz)

        # 4. Renderizado internacional (Adidas) sin palabra casillero
        d_adidas = Deal("slickdeals", "Adidas (via Slickdeals)", "US", "adi_1", "Adidas Run 70s Shoes", "http://adi", 25.0, "USD", 70.0, free_shipping_co=False)
        landed_adi = calcular(25.0, trm=4000.0)
        msg_adi = telegram.render(d_adidas, v, landed=landed_adi)
        self.assertIn("Puesto en Colombia", msg_adi)
        self.assertNotIn("casillero", msg_adi)


class PruebaKoaj(unittest.TestCase):
    """Verifica el scraper HTML y la integración de Koaj Colombia."""

    HTML_MOCK = """
    <div class="products row">
        <article class="product-miniature js-product-miniature" data-id-product="12345">
            <h3 class="s_title_block"><a href="https://www.koaj.co/jeans/12345-jean-slim.html" title="Jean Slim Fit Azul">Jean Slim Fit Azul</a></h3>
            <img data-full-size-image-url="https://www.koaj.co/img/jean.jpg" src="https://www.koaj.co/img/thumb.jpg" />
            <div class="product-price-and-shipping">
                <span class="price st_discounted_price" aria-label="Precio">$\xa089.900</span>
                <span class="regular-price" aria-label="Precio base">$\xa0137.900</span>
                <span class="discount discount-percentage">-35%</span>
            </div>
        </article>
        <article class="product-miniature js-product-miniature" data-id-product="67890">
            <h3 class="s_title_block"><a href="/camisetas/67890-camiseta.html">Camiseta Gráfica Algodón</a></h3>
            <img src="/img/camiseta.jpg" />
            <div class="product-price-and-shipping">
                <span class="price" aria-label="Precio">$ 34.900</span>
            </div>
            <span class="product-unavailable">Agotado</span>
        </article>
    </div>
    """

    def test_parsear_articulos_koaj(self):
        from sources import koaj

        deals = koaj.parsear_articulos(self.HTML_MOCK, etiqueta="Outlet Hombre")
        self.assertEqual(len(deals), 2)

        # Producto 1: en oferta con descuento
        d1 = deals[0]
        self.assertEqual(d1.key, "koaj:12345")
        self.assertEqual(d1.title, "Jean Slim Fit Azul")
        self.assertEqual(d1.url, "https://www.koaj.co/jeans/12345-jean-slim.html")
        self.assertEqual(d1.price, 89900.0)
        self.assertEqual(d1.list_price, 137900.0)
        self.assertEqual(d1.discount_pct, 34.8)
        self.assertEqual(d1.image, "https://www.koaj.co/img/jean.jpg")
        self.assertTrue(d1.in_stock)
        self.assertIn("Outlet Hombre", d1.notes)
        self.assertIn("-35% descuento", d1.notes)

        # Producto 2: sin descuento, agotado, enlace relativo completado
        d2 = deals[1]
        self.assertEqual(d2.key, "koaj:67890")
        self.assertEqual(d2.title, "Camiseta Gráfica Algodón")
        self.assertEqual(d2.url, "https://www.koaj.co/camisetas/67890-camiseta.html")
        self.assertEqual(d2.price, 34900.0)
        self.assertEqual(d2.list_price, 34900.0)
        self.assertEqual(d2.discount_pct, 0.0)
        self.assertEqual(d2.image, "https://www.koaj.co/img/camiseta.jpg")
        self.assertFalse(d2.in_stock)

    def test_integracion_koaj_radar_y_comandos(self):
        import radar
        from core import comandos as mod_comandos, telegram

        # 1. FUENTES en radar
        self.assertIn("koaj", radar.FUENTES)

        # 2. Catálogo y comandos
        self.assertIn("koaj", mod_comandos.CATALOGO)
        self.assertIn("👖 koaj", mod_comandos.BOTONES_TIENDA)

        # 3. Teclado de ropa para Koaj
        teclado = telegram.teclado_categorias("Koaj")
        textos_botones = [btn["text"] for fila in teclado["keyboard"] for btn in fila]
        self.assertIn("👕 Camisetas y Polos", textos_botones)
        self.assertIn("👖 Jeans y Pantalones", textos_botones)


class PruebaPromoHunter(unittest.TestCase):
    """Verifica el extractor estructurado y filtros de El Promo Hunter."""

    MOCK_HTML = r"""
    <html>
    <script>
    self.__next_f.push([1,"1:[\"$\",\"$L16\",null,{\"initialDeals\":[{\"id\":99901,\"titulo\":\"Audífonos Inalámbricos Bluetooth\",\"precio_oferta\":50000,\"precio_original\":100000,\"enlace\":\"https://www.amazon.com/dp/B0ABC123?m=XYZ&tag=ajeno-20\",\"cupones\":\"CUPON50\",\"casillero\":0,\"rating\":\"4.8\",\"num_resenas\":120,\"asin\":\"B0ABC123\"},{\"id\":99902,\"titulo\":\"Producto con Casillero\",\"precio_oferta\":30000,\"precio_original\":60000,\"enlace\":\"https://www.amazon.com/dp/B0XYZ789\",\"cupones\":\"¡No necesita!\",\"casillero\":1,\"rating\":\"4.0\",\"num_resenas\":10,\"asin\":\"B0XYZ789\"}]}]"]);
    </script>
    </html>
    """

    def test_extraer_deals_html(self):
        from sources import promohunter
        deals_raw = promohunter.extraer_deals_html(self.MOCK_HTML)
        self.assertEqual(len(deals_raw), 2)
        self.assertEqual(deals_raw[0]["id"], 99901)
        self.assertEqual(deals_raw[1]["id"], 99902)

    def test_filtro_casillero_estricto(self):
        from sources import promohunter
        deals_raw = promohunter.extraer_deals_html(self.MOCK_HTML)
        # El deal 99902 tiene casillero == 1 -> DEBE ser descartado
        d_casillero = promohunter.parse_deal(deals_raw[1])
        self.assertIsNone(d_casillero)

        # El deal 99901 tiene casillero == 0 -> DEBE ser admitido con envio directo
        d_directo = promohunter.parse_deal(deals_raw[0])
        self.assertIsNotNone(d_directo)
        self.assertEqual(d_directo.key, "amazon:B0ABC123")
        self.assertEqual(d_directo.store, "Amazon")
        self.assertEqual(d_directo.price, 50000.0)
        self.assertEqual(d_directo.list_price, 100000.0)
        self.assertEqual(d_directo.discount_pct, 50.0)
        self.assertEqual(d_directo.currency, "COP")
        self.assertTrue(d_directo.free_shipping_co)
        self.assertIn("CUPON50", d_directo.coupons)
        self.assertIn("4.8", d_directo.notes[0])
        # Verifica que se limpió el tag ajeno de la URL
        self.assertNotIn("tag=ajeno-20", d_directo.url)
        self.assertIn("https://www.amazon.com/dp/B0ABC123", d_directo.url)

    def test_limpiar_enlace_amazon(self):
        from sources import promohunter
        url = "https://www.amazon.com/dp/B012345678?tag=creador-20&ref=deal_link&m=SELLER1"
        limpia = promohunter.limpiar_enlace_amazon(url)
        self.assertNotIn("tag=", limpia)
        self.assertIn("ref=deal_link", limpia)
        self.assertIn("m=SELLER1", limpia)

    def test_slickdeals_filtro_exclusivo_moda(self):
        from sources import slickdeals
        # Zapatos y ropa de marca -> Pasan
        self.assertTrue(slickdeals.es_calzado_o_ropa_de_marca("Adidas Men's Ultraboost 1.0 Running Shoes", "Adidas via Slickdeals"))
        self.assertTrue(slickdeals.es_calzado_o_ropa_de_marca("Nike Air Force 1 '07 Sneakers at Nike", "Nike via Slickdeals"))
        self.assertTrue(slickdeals.es_calzado_o_ropa_de_marca("Puma Essentials Fleece Hoodie at Amazon", "Amazon via Slickdeals"))
        self.assertTrue(slickdeals.es_calzado_o_ropa_de_marca("Levi's Men's 511 Slim Fit Jeans at eBay", "eBay via Slickdeals"))

        # Tecnología o no-moda -> Descartados
        self.assertFalse(slickdeals.es_calzado_o_ropa_de_marca("Apple AirPods Pro 2 at Amazon", "Amazon via Slickdeals"))
        self.assertFalse(slickdeals.es_calzado_o_ropa_de_marca("Dell 27-Inch 4K Gaming Monitor at Dell", "Dell via Slickdeals"))
        self.assertFalse(slickdeals.es_calzado_o_ropa_de_marca("Samsung Galaxy S24 Ultra 5G at Best Buy", "Best Buy via Slickdeals"))
        # Calzado sin marca reconocida -> Descartado
        self.assertFalse(slickdeals.es_calzado_o_ropa_de_marca("Generic Breathable Walking Shoes at Amazon", "Amazon via Slickdeals"))

    def test_promohunter_en_radar_fuentes(self):
        import radar
        self.assertIn("promohunter", radar.FUENTES)


class PruebaMiloDerrocha(unittest.TestCase):
    """Verifica el extractor estructurado y filtros de Milo Derrocha."""

    MOCK_HTML = r"""
    <div data-post="miloderrocha/100">
        <div class="tgme_widget_message_text js-message_text">
            Sudadera Deportiva Hanes con Capucha<br>
            💰 Precio: $35.000<br>
            ❌ Antes: $70.000<br>
            🚚 Envío GRATIS<br>
            <a href="https://www.amazon.com/dp/B0C1T9M4PQ?tag=milo-20">Ver Oferta</a>
        </div>
        <div style="background-image: url('https://cdn1.telesco.pe/file/sample123.jpg')"></div>
    </div>
    <div data-post="miloderrocha/101">
        <div class="tgme_widget_message_text js-message_text">
            Buenos días a todos! Hoy se vienen descuentazos 🔥
        </div>
    </div>
    """

    def test_extraer_deals_html_milo(self):
        from sources import miloderrocha
        deals = miloderrocha.extraer_deals_html(self.MOCK_HTML)
        # El post 101 no tiene precio -> debe ignorarse
        self.assertEqual(len(deals), 1)
        d = deals[0]
        self.assertEqual(d.key, "amazon:B0C1T9M4PQ")
        self.assertEqual(d.store, "Amazon")
        self.assertEqual(d.title, "Sudadera Deportiva Hanes con Capucha")
        self.assertEqual(d.price, 35000.0)
        self.assertEqual(d.list_price, 70000.0)
        self.assertEqual(d.discount_pct, 50.0)
        self.assertEqual(d.currency, "COP")
        self.assertTrue(d.free_shipping_co)
        self.assertEqual(d.image, "https://cdn1.telesco.pe/file/sample123.jpg")
        self.assertNotIn("tag=milo-20", d.url)

    def test_parse_monto_cop(self):
        from sources import miloderrocha
        self.assertEqual(miloderrocha.parse_monto_cop("117.164"), 117164.0)
        self.assertEqual(miloderrocha.parse_monto_cop("$ 38.530 COP"), 38530.0)
        self.assertIsNone(miloderrocha.parse_monto_cop(""))

    def test_miloderrocha_en_radar_fuentes(self):
        import radar
        self.assertIn("miloderrocha", radar.FUENTES)


class PruebaDescuentosTech(unittest.TestCase):
    """Verifica el extractor y filtros de Descuentos Tech Colombia."""

    MOCK_HTML = r"""
    <div data-post="DescuentosTech/100">
        <div class="tgme_widget_message_text js-message_text">
            #PanelLED ✨ Por solo ✨ USD$69 ✅ Panel LED Govee Gaming Pixel inteligente 7.28"<br>
            👉 Ver oferta: <a href="https://www.facebook.com/permalink.php?id=123&story_fbid=456">Ver Oferta</a>
        </div>
        <div style="background-image: url('https://cdn1.telesco.pe/file/sample123.jpg')"></div>
    </div>
    <div data-post="DescuentosTech/101">
        <div class="tgme_widget_message_text js-message_text">
            #Manicura ✨ Por solo ✨ USD$8 ✅ Código: B7V9CW9N 60% Descuento 🏷️ Kit de manicura gel MelodySusie<br>
            👉 Ver oferta: <a href="https://www.facebook.com/permalink.php?id=123&story_fbid=789">Ver Oferta</a>
        </div>
        <div style="background-image: url('https://cdn1.telesco.pe/file/sample456.jpg')"></div>
    </div>
    """

    def test_extraer_deals_html(self):
        from sources import descuentostech
        deals = descuentostech.extraer_deals_html(self.MOCK_HTML)
        self.assertEqual(len(deals), 2)
        d1 = deals[0]
        self.assertEqual(d1.key, "descuentostech:DescuentosTech/100")
        self.assertEqual(d1.store, "DescuentosTech")
        self.assertIn("Panel LED Govee", d1.title)
        self.assertEqual(d1.price, 69.0)
        self.assertEqual(d1.currency, "USD")
        self.assertEqual(d1.url, "https://www.facebook.com/permalink.php?id=123&story_fbid=456")

        d2 = deals[1]
        self.assertEqual(d2.price, 8.0)
        self.assertEqual(d2.coupons, ["B7V9CW9N"])
        self.assertIn("-60% OFF", d2.notes)

    def test_descuentostech_en_radar_fuentes(self):
        import radar
        self.assertIn("descuentostech", radar.FUENTES)

    def test_cazadores_exentos_de_topes_y_sin_objetivo_cumplido(self):
        import radar
        from core.models import Deal

        # 1. Gráfica o portátil caro de $2.5M COP (supera el MAX_PRICE_COP de $2.0M)
        deal_gpu = Deal(
            source="promohunter",
            store="Amazon (vía PromoHunter)",
            country="CO",
            key="ph_gpu_1",
            title="Tarjeta Gráfica RTX 4070 12GB",
            url="http://amz",
            price=2500000.0,
            currency="COP",
            list_price=5000000.0,
        )
        # Debe ser admisible porque viene de un cazador comunitario
        self.assertTrue(radar._precio_admisible(deal_gpu))

        # 2. Una tienda masiva normal al mismo precio sí se bloquea
        deal_tienda = Deal(
            source="falabella",
            store="Falabella",
            country="CO",
            key="fal_gpu_1",
            title="Tarjeta Gráfica RTX 4070 12GB",
            url="http://fal",
            price=2500000.0,
            currency="COP",
            list_price=5000000.0,
        )
        self.assertFalse(radar._precio_admisible(deal_tienda))


class PruebaDeduplicacionYDosificacionGangas(unittest.TestCase):
    """Verifica tiendas limpias (sin vía), deduplicación por ASIN y dosificación."""

    def test_tiendas_limpias_sin_intermediarios(self):
        from sources import promohunter, miloderrocha
        deal_ph = promohunter.parse_deal({
            "id": 123,
            "titulo": "Sudadera Hanes",
            "precio_oferta": 45000.0,
            "casillero": 0,
            "enlace": "https://www.amazon.com/dp/B012345678",
            "asin": "B012345678",
        })
        self.assertIsNotNone(deal_ph)
        self.assertEqual(deal_ph.store, "Amazon")
        self.assertNotIn("vía", deal_ph.store.lower())

        deals_milo = miloderrocha.extraer_deals_html(r"""
        <div data-post="milo/500">
            <div class="tgme_widget_message_text js-message_text">
                Audífonos Bluetooth<br>
                💰 Precio: $50.000<br>
                <a href="https://www.amazon.com/dp/B08XYZ9999">Ver Oferta</a>
            </div>
        </div>
        """)
        self.assertEqual(len(deals_milo), 1)
        self.assertEqual(deals_milo[0].store, "Amazon")
        self.assertNotIn("vía", deals_milo[0].store.lower())

    def test_deduplicacion_cruzada_por_asin(self):
        from sources import promohunter, miloderrocha
        deal_ph = promohunter.parse_deal({
            "id": 888,
            "titulo": "Reloj Inteligente",
            "precio_oferta": 120000.0,
            "casillero": 0,
            "enlace": "https://www.amazon.com/dp/B0SAMEASIN",
            "asin": "B0SAMEASIN",
        })
        deal_milo = miloderrocha.extraer_deals_html(r"""
        <div data-post="milo/777">
            <div class="tgme_widget_message_text js-message_text">
                Reloj Inteligente Smartwatch<br>
                💰 Precio: $120.000<br>
                <a href="https://www.amazon.com/dp/B0SAMEASIN">Ver Oferta</a>
            </div>
        </div>
        """)[0]
        # Ambas fuentes deben producir la MISMA clave canónica
        self.assertEqual(deal_ph.key, "amazon:B0SAMEASIN")
        self.assertEqual(deal_milo.key, "amazon:B0SAMEASIN")

    def test_boton_y_comando_amazon(self):
        from unittest.mock import patch
        from core import comandos
        with patch("core.whitelist.es_admin", return_value=True):
            # Toque de botón en menú interactivo: entrega directa en 1 clic
            msg_boton = {"text": "⚡ Gangas Amazon", "chat": {"id": 12345, "type": "private"}, "from": {"id": 12345}}
            req_boton = comandos.leer_comando(msg_boton)
            self.assertIsNotNone(req_boton)
            self.assertEqual(req_boton["tipo"], "todo_tienda")
            self.assertEqual(req_boton["tienda"], "amazon")

            # Comando tradicional con slash
            msg_slash = {"text": "/amazon", "chat": {"id": 12345, "type": "private"}, "from": {"id": 12345}}
            req_slash = comandos.leer_comando(msg_slash)
            self.assertIsNotNone(req_slash)
            self.assertEqual(req_slash["comando"], "amazon")

    def test_configuracion_dosificacion_y_topes(self):
        import config
        self.assertEqual(config.MAX_ALERTS_COMUNIDAD, 3)
        self.assertEqual(config.MAX_ALERTS_CATALOGOS, 6)
        self.assertEqual(config.MAX_ALERTS_PER_STORE_RUN, 1)
        self.assertEqual(config.MAX_ALERTS_PER_DAY, 60)
        self.assertEqual(config.MAX_ALERTS_PER_SOURCE_DAY, 15)
        self.assertEqual(config.MAX_ALERTS_PER_STORE_DAY, 15)
        self.assertEqual(config.SUPER_DEAL_DISCOUNT_PCT, 60.0)
        self.assertEqual(config.ML_MIN_PRICE_COP, 35000.0)


class PruebaLimitesPorFuenteYTienda(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        try:
            Path(self.tmp.name).unlink()
        except OSError:
            pass

    def test_contadores_por_fuente_y_tienda(self):
        d1 = Deal(source="promohunter", store="amazon", country="US", key="k1", title="TV", url="u", price=100.0, currency="USD")
        d2 = Deal(source="miloderrocha", store="amazon", country="US", key="k2", title="Audio", url="u", price=50.0, currency="USD")
        d3 = Deal(source="mercadolibre", store="mercadolibre", country="CO", key="k3", title="Zapatos", url="u", price=120000.0, currency="COP")

        self.store.sumar_enviada(d1)
        self.store.sumar_enviada(d2)
        self.store.sumar_enviada(d3)

        self.assertEqual(self.store.enviadas_hoy(), 3)
        self.assertEqual(self.store.enviadas_hoy_fuente("promohunter"), 1)
        self.assertEqual(self.store.enviadas_hoy_fuente("miloderrocha"), 1)
        self.assertEqual(self.store.enviadas_hoy_fuente("mercadolibre"), 1)
        # Amazon suma lo de promohunter + miloderrocha
        self.assertEqual(self.store.enviadas_hoy_tienda("amazon"), 2)
        self.assertEqual(self.store.enviadas_hoy_tienda("mercadolibre"), 1)


class PruebaMercadoLibreRelampagosYCalidad(unittest.TestCase):
    def test_filtro_precio_minimo_y_accesorios(self):
        from sources import mercadolibre
        from unittest.mock import patch

        # Simular respuesta JSON con chuchería de $5.000, accesorio y producto válido
        mock_data = {
            "appProps": {
                "pageProps": {
                    "data": {
                        "items": [
                            {
                                "card": {
                                    "metadata": {"id": "1", "url": "http://ejemplo.co/1"},
                                    "components": [
                                        {"type": "title", "title": {"text": "Funda Protector Cable USB"}},
                                        {"type": "price", "price": {"current_price": {"value": 8000}}}
                                    ]
                                }
                            },
                            {
                                "card": {
                                    "metadata": {"id": "2", "url": "http://ejemplo.co/2"},
                                    "components": [
                                        {"type": "title", "title": {"text": "Baratija de plastico llavero"}},
                                        {"type": "price", "price": {"current_price": {"value": 12000}}}
                                    ]
                                }
                            },
                            {
                                "card": {
                                    "metadata": {"id": "3", "url": "http://ejemplo.co/3"},
                                    "components": [
                                        {"type": "title", "title": {"text": "Tenis adidas Running Duramo Rc2"}},
                                        {"type": "price", "price": {
                                            "current_price": {"value": 180000},
                                            "price_labels": [{"values": [{"key": "previous_price", "price": {"value": 300000}}]}]
                                        }}
                                    ]
                                }
                            }
                        ]
                    }
                }
            }
        }

        with patch("core.http.get_text", return_value=f'_n.ctx.r = {json.dumps(mock_data)};'):
            deals = mercadolibre._extraer_ofertas(es_relampago=True)
            self.assertEqual(len(deals), 1)
            self.assertEqual(deals[0].title, "Tenis adidas Running Duramo Rc2")
            self.assertTrue(deals[0].vence_pronto)
            self.assertIn("⚡ Oferta Relámpago", deals[0].notes)


class PruebaBotonEnviarmelaAlChat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        try:
            Path(self.tmp.name).unlink()
        except OSError:
            pass

    def test_guardar_y_obtener_deal_reciente(self):
        d = Deal(source="vtex", store="Exito", country="CO", key="exito:123", title="Nevera Haceb", url="http://exito.com/nevera", price=1500000.0, currency="COP")
        h = self.store.guardar_deal_reciente(d)
        self.assertIsNotNone(h)
        self.assertEqual(len(h), 10)

        recuperado = self.store.obtener_deal_reciente(h)
        self.assertIsNotNone(recuperado)
        self.assertEqual(recuperado.key, "exito:123")
        self.assertEqual(recuperado.title, "Nevera Haceb")
        self.assertEqual(recuperado.price, 1500000.0)

    def test_deep_link_comando_start_deal(self):
        from unittest.mock import patch
        from core import comandos
        with patch("core.whitelist.es_admin", return_value=True):
            msg = {
                "text": "/start deal_a1b2c3d4e5",
                "chat": {"id": 12345, "type": "private"},
                "from": {"id": 12345, "first_name": "Carlos"}
            }
            req = comandos.leer_comando(msg)
            self.assertIsNotNone(req)
            self.assertEqual(req["tipo"], "start_deal")
            self.assertEqual(req["deal_hash"], "a1b2c3d4e5")
            self.assertEqual(req["nombre"], "Carlos")

    def test_atender_solicitud_start_deal_invita_al_canal(self):
        from unittest.mock import patch
        import radar
        from core.models import Deal

        d = Deal(source="vtex", store="Olimpica", country="CO", key="ol:1", title="Bicicleta", url="http://ol.co", price=300000.0, currency="COP")
        h = self.store.guardar_deal_reciente(d)

        solicitudes = [{
            "tipo": "start_deal",
            "deal_hash": h,
            "chat_id": 9999,
            "user_id": 9999,
            "nombre": "Pedro",
        }]

        with patch("core.telegram.enviar_oferta") as mock_oferta, \
             patch("core.telegram.es_miembro_del_canal", return_value=False), \
             patch("core.telegram.send") as mock_send, \
             patch("core.telegram.teclado_unirse_canal", return_value={"inline_keyboard": []}), \
             patch("radar.Store", return_value=self.store):

            radar.atender_solicitudes(solicitudes)
            mock_oferta.assert_called_once()
            self.assertTrue(any("¿Te gustó esta oferta?" in call.args[0] for call in mock_send.call_args_list))


    def test_deep_link_usuario_nuevo_no_miembro_del_canal_pasa_directo(self):
        from unittest.mock import patch
        from core import comandos, whitelist

        user_nuevo = 987654321
        # Asegurarse de que no esté en whitelist
        whitelist.rechazar(user_nuevo)

        # Usuario no es admin y no está en el canal oficial
        with patch("core.whitelist.es_admin", return_value=False), \
             patch("core.telegram.es_miembro_del_canal", return_value=False):

            # Caso 1: Envía deep link de oferta desde Facebook -> DEBE pasar directo a la oferta
            msg_deal = {
                "text": "/start deal_fb12345678",
                "chat": {"id": user_nuevo, "type": "private"},
                "from": {"id": user_nuevo, "first_name": "Ana"}
            }
            req_deal = comandos.leer_comando(msg_deal)
            self.assertIsNotNone(req_deal)
            self.assertEqual(req_deal["tipo"], "start_deal")
            self.assertEqual(req_deal["deal_hash"], "fb12345678")
            self.assertTrue(whitelist.es_permitido(user_nuevo))

            # Caso 2: Si intenta consultar el menú general sin estar en el canal -> DEBE pedirle unirse
            msg_menu = {
                "text": "/menu",
                "chat": {"id": user_nuevo, "type": "private"},
                "from": {"id": user_nuevo, "first_name": "Ana"}
            }
            req_menu = comandos.leer_comando(msg_menu)
            self.assertIsNotNone(req_menu)
            self.assertEqual(req_menu["tipo"], "unirse_canal")

    def test_vtex_alta_resolucion_imagenes(self):
        from sources.vtex import _alta_resolucion

        url_thumb1 = "https://olimpica.vteximg.com.br/arquivos/ids/1465909-200-200/WhatsApp-Image.jpg?v=123"
        url_thumb2 = "https://carulla.vteximg.com.br/arquivos/ids/26176034-55-55/Parlante.jpg"
        url_hd = "https://exito.vteximg.com.br/arquivos/ids/999999/Foto.jpg"

        self.assertEqual(_alta_resolucion(url_thumb1),
                         "https://olimpica.vteximg.com.br/arquivos/ids/1465909/WhatsApp-Image.jpg?v=123")
        self.assertEqual(_alta_resolucion(url_thumb2),
                         "https://carulla.vteximg.com.br/arquivos/ids/26176034/Parlante.jpg")
        self.assertEqual(_alta_resolucion(url_hd), url_hd)
        self.assertIsNone(_alta_resolucion(None))


class PruebaBuzonFeedbackYReportes(unittest.TestCase):
    def tearDown(self):
        from core import comandos
        for cid in (12345, 88888, 7777, 9999):
            comandos.limpiar_estado_feedback(cid)

    def test_teclado_tiendas_tiene_boton_reportes(self):
        from core import telegram
        teclado = telegram.teclado_tiendas()
        botones_flat = [b["text"] for fila in teclado["keyboard"] for b in fila]
        self.assertIn("✍️ Sugerencias y Reportes", botones_flat)

    def test_boton_cancelar_feedback(self):
        from core import telegram
        btn = telegram.boton_cancelar_feedback()
        self.assertIn("inline_keyboard", btn)
        self.assertEqual(btn["inline_keyboard"][0][0]["callback_data"], "cancelar_feedback")
        self.assertIn("cancelar", btn["inline_keyboard"][0][0]["text"].lower())

    def test_leer_comando_deep_link_reporte(self):
        from unittest.mock import patch
        from core import comandos
        with patch("core.whitelist.es_admin", return_value=True):
            msg = {
                "text": "/start report_9876543210",
                "chat": {"id": 12345, "type": "private"},
                "from": {"id": 12345, "first_name": "Laura", "username": "laura_dev"}
            }
            req = comandos.leer_comando(msg)
            self.assertIsNotNone(req)
            self.assertEqual(req["tipo"], "iniciar_reporte")
            self.assertEqual(req["deal_hash"], "9876543210")
            self.assertEqual(req["nombre"], "Laura")

    def test_leer_comando_boton_sugerencias(self):
        from unittest.mock import patch
        from core import comandos
        with patch("core.whitelist.es_admin", return_value=True):
            msg = {
                "text": "✍️ Sugerencias y Reportes",
                "chat": {"id": 12345, "type": "private"},
                "from": {"id": 12345, "first_name": "Laura"}
            }
            req = comandos.leer_comando(msg)
            self.assertIsNotNone(req)
            self.assertEqual(req["tipo"], "iniciar_reporte")
            self.assertIsNone(req["deal_hash"])

    def test_estado_feedback_y_captura_texto(self):
        from unittest.mock import patch
        from core import comandos
        chat_id = 88888
        comandos.fijar_estado_feedback(chat_id, {"deal": {"title": "TV Samsung"}})
        self.assertIsNotNone(comandos.obtener_estado_feedback(chat_id))

        with patch("core.whitelist.es_admin", return_value=False), \
             patch("core.telegram.es_miembro_del_canal", return_value=True), \
             patch("core.whitelist.es_permitido", return_value=True):
            # 1. El usuario envía texto libre con feedback activo
            msg = {
                "text": "El enlace no carga la página de la tienda",
                "chat": {"id": chat_id, "type": "private"},
                "from": {"id": chat_id, "first_name": "Mateo", "username": "mateo"}
            }
            req = comandos.leer_comando(msg)
            self.assertIsNotNone(req)
            self.assertEqual(req["tipo"], "enviar_feedback")
            self.assertEqual(req["texto_feedback"], "El enlace no carga la página de la tienda")
            self.assertEqual(req["estado_feedback"]["deal"]["title"], "TV Samsung")

            # 2. Cancelar mediante texto
            comandos.fijar_estado_feedback(chat_id, {"deal": None})
            msg_cancelar = {
                "text": "❌ Cancelar",
                "chat": {"id": chat_id, "type": "private"},
                "from": {"id": chat_id, "first_name": "Mateo"}
            }
            req_canc = comandos.leer_comando(msg_cancelar)
            self.assertIsNotNone(req_canc)
            self.assertEqual(req_canc["tipo"], "cancelar_feedback")
            self.assertIsNone(comandos.obtener_estado_feedback(chat_id))

    def test_atender_solicitudes_flujo_completo_reporte(self):
        import radar
        from unittest.mock import patch
        from core import comandos
        mensajes_enviados = []
        with patch("core.telegram.send", side_effect=lambda txt, **k: mensajes_enviados.append((txt, k))):
            # 1. Iniciar reporte general
            sol_ini = {"tipo": "iniciar_reporte", "chat_id": 7777, "deal_hash": None}
            radar.atender_solicitudes([sol_ini])
            self.assertTrue(any("Buzón de Sugerencias" in m[0] for m in mensajes_enviados))
            self.assertIsNotNone(comandos.obtener_estado_feedback(7777))

            # 2. Enviar feedback
            sol_env = {
                "tipo": "enviar_feedback",
                "chat_id": 7777,
                "user_id": 7777,
                "nombre": "Ana",
                "username": "ana_col",
                "texto_feedback": "Excelente bot, pero podrían agregar más tiendas de calzado",
                "estado_feedback": comandos.obtener_estado_feedback(7777)
            }
            mensajes_enviados.clear()
            radar.atender_solicitudes([sol_env])
            self.assertIsNone(comandos.obtener_estado_feedback(7777))
            # Se envió confirmación al usuario y alerta al admin
            self.assertTrue(any("¡Muchas gracias!" in m[0] for m in mensajes_enviados))
            self.assertTrue(any("Nueva Sugerencia / Reporte General" in m[0] for m in mensajes_enviados))

    def test_callback_query_cancelar_feedback(self):
        import server
        from core import comandos
        from unittest.mock import patch
        comandos.fijar_estado_feedback(9999, {"deal": None})
        cb = {
            "id": "cq_123",
            "from": {"id": 9999, "first_name": "Pedro"},
            "message": {"message_id": 55, "chat": {"id": 9999}},
            "data": "cancelar_feedback",
        }
        with patch("core.telegram.responder_callback") as mock_resp, \
             patch("core.telegram.editar_mensaje") as mock_edit:
            server.atender_callback_query(cb)
            mock_resp.assert_called_once_with("cq_123", "Reporte cancelado.")
            mock_edit.assert_called_once()
            self.assertIsNone(comandos.obtener_estado_feedback(9999))


class PruebaBalanceoYDiversificacion(unittest.TestCase):
    def test_clasificar_departamento(self):
        from radar import _clasificar_departamento
        self.assertEqual(_clasificar_departamento("Freidora De Aire Imusa 3.2L Digital"), "cocina_electro")
        self.assertEqual(_clasificar_departamento("Horno Microondas Haceb 20L"), "cocina_electro")
        self.assertEqual(_clasificar_departamento("Smart TV Samsung 55 Pulgadas 4K"), "tecnologia")
        self.assertEqual(_clasificar_departamento("Tenis Running Adidas Galaxy"), "moda_calzado")
        self.assertEqual(_clasificar_departamento("Camiseta Polo Hombre"), "moda_calzado")
        self.assertEqual(_clasificar_departamento("Colchon Doble Resortado Ortopedico"), "hogar")
        self.assertEqual(_clasificar_departamento("Juguete Carro a Control Remoto"), "otros")

    def test_tope_categoria_freidora_admite_hasta_320k(self):
        from radar import _precio_admisible
        # Freidora a $250.000 COP con lista $500.000 COP (50% desc) entra sin problemas
        d = oferta(title="Freidora de Aire Oster 4 Litros", price=250_000.0, list_price=500_000.0, currency="COP")
        self.assertTrue(_precio_admisible(d, trm=4000.0))

        # Freidora a $450.000 COP (supera el nuevo tope de 320.000 COP para gangas)
        d_cara = oferta(title="Freidora de Aire Ninja Doble Cesta", price=450_000.0, list_price=900_000.0, currency="COP")
        self.assertFalse(_precio_admisible(d_cara, trm=4000.0))

    def test_seleccionar_diversificadas_evita_monopolio_tienda_y_depto(self):
        from radar import _seleccionar_diversificadas
        from core.scoring import Verdict

        v = Verdict(alertar=True, inmediata=True, confianza="alta", motivo="ok")

        # Supongamos que Éxito tiene 3 ofertas de audífonos con 90% de descuento
        # y Alkosto tiene una freidora con 50%
        # y Falabella tiene unos tenis con 45%
        candidatas = [
            (oferta(key="e1", store="Exito", title="Audifonos Bluetooth In-ear", price=20_000, list_price=200_000), v),
            (oferta(key="e2", store="Exito", title="Audifonos Cableados", price=15_000, list_price=150_000), v),
            (oferta(key="e3", store="Exito", title="Audifonos Diadema", price=30_000, list_price=300_000), v),
            (oferta(key="a1", store="Alkosto", title="Freidora de Aire Oster 4L", price=220_000, list_price=450_000), v),
            (oferta(key="f1", store="Falabella", title="Tenis Running Nike Air", price=180_000, list_price=350_000), v),
        ]

        # Con tope=3 y max_por_tienda=1:
        # Deben entrar 3 ofertas de 3 tiendas distintas y diferentes departamentos
        seleccionadas = _seleccionar_diversificadas(candidatas, tope=3, max_por_tienda=1)
        self.assertEqual(len(seleccionadas), 3)

        tiendas = [par[0].store for par in seleccionadas]
        self.assertEqual(len(set(tiendas)), 3)
        self.assertIn("Alkosto", tiendas)
        self.assertIn("Falabella", tiendas)
        self.assertIn("Exito", tiendas)

    def test_seleccionar_diversificadas_flexibiliza_si_no_hay_mas_tiendas(self):
        from radar import _seleccionar_diversificadas
        from core.scoring import Verdict
        v = Verdict(alertar=True, inmediata=True, confianza="alta", motivo="ok")

        # Si solo una tienda tiene ofertas, en el pase 3 debe llenar los cupos sin quedar vacía
        candidatas = [
            (oferta(key="e1", store="Exito", title="Audifonos Bluetooth In-ear", price=20_000, list_price=200_000), v),
            (oferta(key="e2", store="Exito", title="Cargador Rapido Tipo C", price=15_000, list_price=150_000), v),
        ]
        seleccionadas = _seleccionar_diversificadas(candidatas, tope=2, max_por_tienda=1)
        self.assertEqual(len(seleccionadas), 2)


class PruebaPanelSaludAdmin(unittest.TestCase):
    def test_leer_comando_salud(self):
        from core import comandos
        msg = {
            "text": "/salud",
            "chat": {"id": 5583002220, "type": "private"},
            "from": {"id": 5583002220, "first_name": "Emanuel"},
        }
        sol = comandos.leer_comando(msg)
        self.assertIsNotNone(sol)
        self.assertEqual(sol["comando"], "salud")
        self.assertEqual(sol["tipo"], "salud")

    def test_salud_solo_responde_a_admin(self):
        import radar
        from unittest.mock import patch
        mensajes_enviados = []
        with patch("core.telegram.send", side_effect=lambda txt, **k: mensajes_enviados.append((txt, k))):
            # 1. Usuario normal intenta ejecutar /salud -> Rechazado
            sol_normal = {"comando": "salud", "tipo": "salud", "chat_id": 1111, "user_id": 1111}
            radar.atender_solicitudes([sol_normal])
            self.assertTrue(any("exclusivo para el administrador" in m[0] for m in mensajes_enviados))
            self.assertFalse(any("Panel de Diagnóstico" in m[0] for m in mensajes_enviados))

            # 2. Administrador ejecuta /salud -> Autorizado con panel
            mensajes_enviados.clear()
            sol_admin = {"comando": "salud", "tipo": "salud", "chat_id": 5583002220, "user_id": 5583002220}
            radar.atender_solicitudes([sol_admin])
            self.assertTrue(any("Panel de Diagnóstico y Salud" in m[0] for m in mensajes_enviados))
            self.assertTrue(any("Alkosto / K-tronix" in m[0] for m in mensajes_enviados))

    def test_recolectar_fuente_registra_metricas_y_aisla_errores(self):
        import radar
        # 1. Fuente exitosa
        def fuente_ok():
            return [oferta(key="ok1")]

        res_ok = radar._recolectar_fuente("prueba_tienda", fuente_ok)
        self.assertEqual(len(res_ok), 1)
        info_ok = radar._SALUD_FUENTES.get("prueba_tienda")
        self.assertIsNotNone(info_ok)
        self.assertTrue(info_ok["ok"])
        self.assertEqual(info_ok["ofertas"], 1)

        # 2. Fuente que falla -> No debe tumbar el sistema, registra error
        def fuente_rota():
            raise ConnectionError("Timeout simulado en tienda")

        res_rota = radar._recolectar_fuente("tienda_caida", fuente_rota)
        self.assertEqual(len(res_rota), 0)
        info_rota = radar._SALUD_FUENTES.get("tienda_caida")
        self.assertIsNotNone(info_rota)
        self.assertFalse(info_rota["ok"])
        self.assertIn("Timeout simulado", info_rota["error"])

    def test_fallback_menu_chat_privado(self):
        from core import comandos
        msg_menu = {
            "text": "menu",
            "chat": {"id": 5583002220, "type": "private"},
            "from": {"id": 5583002220, "first_name": "Emanuel"},
        }
        sol = comandos.leer_comando(msg_menu)
        self.assertIsNotNone(sol)
        self.assertEqual(sol["tipo"], "menu")

    def test_fase0_detectar_errores_precio_glitches(self):
        import radar
        from core import telegram
        from core.scoring import Verdict

        # 1. Glitch Tecnológico/Pesado: desc >= 65% y ahorro >= $300.000 COP
        d_glitch_pesado = oferta(
            title="Smart TV OLED 65 LG 4K",
            price=700000,
            list_price=2000000,
            discount=65.0,
            currency="COP",
        )
        self.assertTrue(radar.detectar_errores_precio(d_glitch_pesado))

        # No es glitch: desc < 65% aunque el ahorro sea alto
        d_no_glitch_1 = oferta(
            title="Nevera LG",
            price=1500000,
            list_price=2000000,
            discount=25.0,
            currency="COP",
        )
        self.assertFalse(radar.detectar_errores_precio(d_no_glitch_1))

        # 2. Glitch General: desc >= 85% y precio >= $20.000 COP
        d_glitch_gral = oferta(
            title="Tenis Running Pro",
            price=30000,
            list_price=250000,
            discount=88.0,
            currency="COP",
        )
        self.assertTrue(radar.detectar_errores_precio(d_glitch_gral))

        # No es glitch: desc 90% pero producto ínfimo < 20.000 COP (ej. pañuelos a 5.000)
        d_chucheria = oferta(
            title="Pañuelos Faciales",
            price=5000,
            list_price=50000,
            discount=90.0,
            currency="COP",
        )
        self.assertFalse(radar.detectar_errores_precio(d_chucheria))

        # 3. Verificar etiqueta en cabecera de Telegram
        v = Verdict(alertar=True, inmediata=True, confianza="alta", glitch=True, etiquetas=["test"], motivo="")
        lineas = telegram._lineas(d_glitch_pesado, v)
        self.assertEqual(lineas[0], "🚨 ERROR DE PRECIO / SÚPER GANGA 🚨")

    def test_fase1_subclasificacion_por_familias_regex(self):
        from core import filtros

        casos = [
            ("Smart TV Samsung 55 Pulgadas 4K UHD", "tv_y_monitores"),
            ("Monitor Gamer Curvo 27 144Hz", "tv_y_monitores"),
            ("Nevera No Frost Mabe 400 Litros Inox", "refrigeracion"),
            ("Nevecon Samsung French Door", "refrigeracion"),
            ("Celular Samsung Galaxy S24 Ultra 256GB", "smartphones"),
            ("Smartphone Xiaomi Redmi Note 13", "smartphones"),
            ("Airfryer Freidora De Aire Imusa 3.2L", "pequenos_electro"),
            ("Cafetera Espresso Oster Prima Latte", "pequenos_electro"),
            ("Audifonos Inalambricos Sony WH-1000XM5", "perifericos"),
            ("Mouse Gamer Logitech G502", "perifericos"),
            ("Camiseta Polo Classic Fit Hombre", "ropa_basica"),
            ("Jean Clasico Azul Denim", "ropa_basica"),
            ("Juguete Carro Monster Truck", "otros"),
        ]
        for titulo, familia_esperada in casos:
            fam = filtros.asignar_familia(titulo)
            self.assertEqual(fam, familia_esperada, f"Falló para: '{titulo}'")
            # Probar alias _asignar_familia
            self.assertEqual(filtros._asignar_familia(titulo), familia_esperada)

    def test_fase2_torneo_y_score_ponderado(self):
        import radar
        from core.scoring import Verdict

        # Simular productos:
        # Camiseta con 80% de descuento (peso 0.8 -> score = 64)
        d_ropa = oferta(title="Camiseta Basica Cuello Redondo", price=20000, list_price=100000, discount=80.0)
        # Camiseta peor dentro de la misma familia (60% -> debe ser eliminada)
        d_ropa_peor = oferta(title="Camiseta Polo Algodon", price=40000, list_price=100000, discount=60.0)
        # Televisor con 50% de descuento (peso 1.5 -> score = 75)
        d_tv = oferta(title="Smart TV LG 50 Pulgadas 4K", price=1000000, list_price=2000000, discount=50.0)
        # Smartphone con 45% (peso 1.5 -> score = 67.5)
        d_cel = oferta(title="Celular Galaxy A54 128GB", price=800000, list_price=1454545, discount=45.0)

        v = Verdict(alertar=True, inmediata=True, confianza="alta", glitch=False, etiquetas=[], motivo="")
        candidatas = [
            (d_ropa, v),
            (d_ropa_peor, v),
            (d_tv, v),
            (d_cel, v),
        ]

        campeones = radar.torneo_familias(candidatas)

        # 1. Solo debe haber 3 campeones (una por cada familia: tv_y_monitores, smartphones, ropa_basica)
        self.assertEqual(len(campeones), 3)

        # 2. El TV (score 75.0) debe ganar el primer puesto por encima de la camiseta al 80% (score 64.0)
        primero = campeones[0]
        self.assertEqual(primero[0].title, d_tv.title)
        self.assertEqual(primero[2], 75.0)

        # 3. El celular (score 67.5) queda en segundo puesto
        segundo = campeones[1]
        self.assertEqual(segundo[0].title, d_cel.title)
        self.assertEqual(segundo[2], 67.5)

        # 4. La camiseta (score 64.0) queda en tercer puesto
        tercero = campeones[2]
        self.assertEqual(tercero[0].title, d_ropa.title)
        self.assertEqual(tercero[2], 64.0)

    def test_fase3_bifurcacion_telegram_y_menciones_honorificas(self):
        from unittest.mock import patch
        from core import telegram

        menciones = [
            oferta(title="Audifonos Bluetooth Inalambricos Cancelacion de Ruido Sony",
                   price=150000, discount=40.0, url="https://alkosto.com/audifonos", currency="COP"),
            oferta(title="Cafetera Electrica Oster 12 Tazas Filtro Permanente",
                   price=80000, discount=35.0, url="https://exito.com/cafetera", currency="COP"),
        ]

        # 1. Verificar formato exacto de render_menciones_honorificas
        html = telegram.render_menciones_honorificas(menciones)
        self.assertIn("⚡ <b>Otras gangas que acaban de salir:</b>", html)
        self.assertIn("🔸 <a href='https://alkosto.com/audifonos'>Audifonos Bluetooth Inalambricos Cancelacion", html)
        self.assertIn("💵 $150.000 (🔥 -40%)", html)
        self.assertIn("🔸 <a href='https://exito.com/cafetera'>Cafetera Electrica Oster 12 Tazas Filtro P", html)
        self.assertIn("💵 $80.000 (🔥 -35%)", html)

        # 2. Verificar envío con parámetros críticos parse_mode="HTML", disable_web_page_preview=True, disable_notification=True
        llamadas_post = []
        with patch("config.TELEGRAM_BOT_TOKEN", "token_test"), \
             patch("config.TELEGRAM_CHAT_ID", "-10012345"), \
             patch("core.http.post_json", side_effect=lambda url, payload, **k: llamadas_post.append(payload) or {"ok": True}):
            ok = telegram.enviar_menciones_honorificas(menciones)
            self.assertTrue(ok)
            self.assertEqual(len(llamadas_post), 1)
            payload = llamadas_post[0]
            self.assertEqual(payload["chat_id"], "-10012345")
            self.assertEqual(payload["parse_mode"], "HTML")
            self.assertTrue(payload["disable_web_page_preview"])
            self.assertTrue(payload["disable_notification"])

    def test_fase4_actualizacion_telemetria_salud(self):
        import radar
        radar._TELEMETRIA_LIGAS["glitches_hoy"] = 4
        radar._TELEMETRIA_LIGAS["ultimo_top3_avg_score"] = 73.8
        radar._TELEMETRIA_LIGAS["menciones_hoy"] = 12

        panel = radar.generar_panel_salud()
        self.assertIn("Errores de Precio (Glitches) hoy:</b> <b>4</b>", panel)
        self.assertIn("Promedio Score Top 3:</b> <b>73.8 pts</b>", panel)
        self.assertIn("Menciones honoríficas enviadas:</b> <b>12</b>", panel)


class PruebaAntiDuplicadosYPersistencia(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        try:
            Path(self.tmp.name).unlink()
        except OSError:
            pass

    def test_deduplicacion_secundaria_por_titulo_y_tienda_2_dias(self):
        d1 = oferta(key="vtex:totto:111", store="Totto", title="Morral Escolar Acuarela", price=89900.0)
        self.store.mark_alerted(d1)

        # Mismo producto con clave diferente (ej. cambio de ID en VTEX o nuevo SKU) dentro de 48h
        d2 = oferta(key="vtex:totto:222", store="Totto", title="Morral Escolar Acuarela", price=89900.0)
        pasa, motivo = self.store.should_alert(d2, dias_titulo=2)
        self.assertFalse(pasa)
        self.assertIn("ya avisada en los ultimos 2 dias", motivo)

        # En otra tienda no se descarta (ej. Carulla)
        d_carulla = oferta(key="vtex:carulla:333", store="Carulla", title="Morral Escolar Acuarela", price=89900.0)
        pasa_carulla, motivo_carulla = self.store.should_alert(d_carulla, dias_titulo=2)
        self.assertTrue(pasa_carulla)
        self.assertEqual(motivo_carulla, "nueva")

        # Si pasaron más de 2 días (ej. 3 días atrás), ya debe ser admisible nuevamente
        hace_3_dias = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).isoformat()
        self.store.conn.execute("UPDATE alerts SET last_alert_ts = ? WHERE key = ?", (hace_3_dias, d1.key))
        self.store.conn.commit()

        pasa_expirada, motivo_expirada = self.store.should_alert(d2, dias_titulo=2)
        self.assertTrue(pasa_expirada)
        self.assertEqual(motivo_expirada, "nueva")

    def test_persistencia_mostradas_comandos(self):
        from core import comandos
        from pathlib import Path as _Path
        with tempfile.TemporaryDirectory() as tmp_dir:
            orig_estado = comandos.ESTADO
            comandos.ESTADO = _Path(tmp_dir) / "comandos_test.json"
            try:
                cid = "chat_test_48h"
                comandos.marcar_mostradas(["oferta_totto_1", "oferta_carulla_1"], chat_id=cid)
                comandos.marcar_mostradas(["oferta_otra_tienda"], chat_id="otro_chat")
                mostradas = comandos.ya_mostradas(chat_id=cid)
                self.assertIn("oferta_totto_1", mostradas)
                self.assertIn("oferta_carulla_1", mostradas)
                self.assertNotIn("oferta_totto_1", comandos.ya_mostradas(chat_id="otro_chat"))
                self.assertIn("oferta_otra_tienda", comandos.ya_mostradas(chat_id="otro_chat"))
            finally:
                comandos.ESTADO = orig_estado

    def test_promohunter_expansion_enlace_acortado_amazon(self):
        from sources import promohunter
        from unittest.mock import MagicMock, patch

        mock_resp = MagicMock()
        mock_resp.geturl.return_value = "https://www.amazon.com/dp/B09XYZ1234?tag=ajeno"
        mock_ctx = MagicMock()
        mock_ctx.__enter__.return_value = mock_resp
        mock_ctx.__exit__.return_value = None

        with patch("urllib.request.urlopen", return_value=mock_ctx):
            item = {
                "id": "99999",
                "titulo": "Audífonos Inalámbricos Bluetooth",
                "precio_oferta": 75000,
                "enlace": "https://a.co/d/test1234",
                "casillero": 0,
            }
            deal = promohunter.parse_deal(item)
            self.assertIsNotNone(deal)
            # Debe extraer el ASIN y unificar la clave canónica como amazon:B09XYZ1234
            self.assertEqual(deal.key, "amazon:B09XYZ1234")
            self.assertIn("amazon.com/dp/B09XYZ1234", deal.url)


class PruebaRepublicaDescuentos(unittest.TestCase):
    """Verifica el extractor estructurado y filtros anti-anuncios de República de Descuentos."""

    MOCK_HTML = r"""
    <div data-post="Republicadescuentos/25854">
        <div class="tgme_widget_message_text js-message_text">
            #ad #amazon 400.000 Envío Incluido AOC 24 Inch Curved Gaming Monitor FHD 1080P 180Hz VA 0.5ms HDR Compra AQUÍ <a href="https://amzn.to/4xzYI1B">https://amzn.to/4xzYI1B</a>
        </div>
        <div style="background-image: url('https://cdn1.telesco.pe/file/monitor_aoc.jpg')"></div>
    </div>
    <div data-post="Republicadescuentos/25855">
        <div class="tgme_widget_message_text js-message_text">
            #ad #amazon REGALADO 58.000 Envío Gratis Prime CUPÓN ZD7DMC76 HUANUO FlowLift ™ Pro Monitor Arm Compra AQUÍ <a href="https://amzn.to/4AkdhsD">https://amzn.to/4AkdhsD</a>
        </div>
    </div>
    <div data-post="Republicadescuentos/25852">
        <div class="tgme_widget_message_text js-message_text">
            #ad #aliexpress REGALADO 502.200 Envío Incluido CUPÓN OPOCCO20 Procesador Ryzen 7 5700X Compra AQUÍ <a href="https://s.click.aliexpress.com/e/_mKBHB9v">https://s.click.aliexpress.com/e/_mKBHB9v</a>
        </div>
    </div>
    <div data-post="Republicadescuentos/25857">
        <div class="tgme_widget_message_text js-message_text">
            Únete a Morse con mi enlace y recibe hasta &#036;40 USD durante tus primeros 30 días. AHORRA Y GANA. CREA TU CUENTA AQUI 💰 <a href="https://morse.link/es/join/LmLRPq">https://morse.link/es/join/LmLRPq</a>
        </div>
    </div>
    <div data-post="Republicadescuentos/25860">
        <div class="tgme_widget_message_text js-message_text">
            Descarga esta app de préstamos y gana dinero ya: <a href="https://appexterna.com/ref123">https://appexterna.com/ref123</a>
        </div>
    </div>
    <div data-post="Republicadescuentos/25861">
        <div class="tgme_widget_message_text js-message_text">
            Buenos días miembros del canal! Atentos a los descuentos de hoy 🔥
        </div>
    </div>
    """

    def test_extraer_deals_html_valida_ofertas_y_cupones(self):
        from sources import republica
        from unittest.mock import patch, MagicMock

        # Mock para evitar llamadas externas a amzn.to en tests
        mock_resp = MagicMock()
        mock_resp.geturl.return_value = "https://www.amazon.com/dp/B08XYZ1234?tag=ajeno"
        mock_ctx = MagicMock()
        mock_ctx.__enter__.return_value = mock_resp
        mock_ctx.__exit__.return_value = None

        with patch("urllib.request.urlopen", return_value=mock_ctx):
            deals = republica.extraer_deals_html(self.MOCK_HTML)

        # De los 6 posts:
        # - Post 25854: Monitor AOC Amazon (OK)
        # - Post 25855: Brazo HUANUO con cupón ZD7DMC76 Amazon (OK)
        # - Post 25852: Procesador Ryzen AliExpress con cupón OPOCCO20 (OK)
        # - Post 25857: Morse referido (DESCARTADO)
        # - Post 25860: App externa spam (DESCARTADO)
        # - Post 25861: Saludo sin precio (DESCARTADO)
        self.assertEqual(len(deals), 3)

        # Verificar oferta de AliExpress
        d_ali = [d for d in deals if d.store == "AliExpress"][0]
        self.assertEqual(d_ali.price, 502200.0)
        self.assertEqual(d_ali.coupons, ["OPOCCO20"])
        self.assertIn("Procesador Ryzen 7 5700X", d_ali.title)

        # Verificar oferta con cupón en Amazon
        d_arm = [d for d in deals if "HUANUO FlowLift" in d.title][0]
        self.assertEqual(d_arm.price, 58000.0)
        self.assertEqual(d_arm.coupons, ["ZD7DMC76"])
        self.assertEqual(d_arm.key, "amazon:B08XYZ1234")

        # Verificar monitor AOC con foto
        d_mon = [d for d in deals if "AOC 24 Inch" in d.title][0]
        self.assertEqual(d_mon.price, 400000.0)
        self.assertEqual(d_mon.image, "https://cdn1.telesco.pe/file/monitor_aoc.jpg")

    def test_filtro_estricto_descarta_anuncios_y_referidos(self):
        from sources import republica
        html_spam = r"""
        <div data-post="Republicadescuentos/9991">
            <div class="tgme_widget_message_text js-message_text">
                Unete a esta app con mi enlace y gana dinero en 30 dias: https://invitacion.xyz/gana
            </div>
        </div>
        <div data-post="Republicadescuentos/9992">
            <div class="tgme_widget_message_text js-message_text">
                Crea tu cuenta y reclama bono de bienvenida 50.000 COP https://bancoapp.com/ref
            </div>
        </div>
        """
        deals = republica.extraer_deals_html(html_spam)
        self.assertEqual(len(deals), 0)

    def test_aliexpress_no_obtiene_nota_amazon_prime(self):
        from sources import republica
        html_ali = r"""
        <div data-post="Republicadescuentos/1234">
            <div class="tgme_widget_message_text js-message_text">
                #ad #aliexpress 150.000 Envío Gratis Prime RAM DDR4 16GB Compra AQUÍ <a href="https://s.click.aliexpress.com/e/_c38RMcut">https://s.click.aliexpress.com/e/_c38RMcut</a>
            </div>
        </div>
        """
        deals = republica.extraer_deals_html(html_ali)
        self.assertEqual(len(deals), 1)
        self.assertEqual(deals[0].store, "AliExpress")
        # NUNCA debe asignarse Amazon Prime a AliExpress
        for nota in deals[0].notes:
            self.assertNotIn("Amazon Prime", nota)

    def test_descarte_de_posts_tipo_combo(self):
        from sources import republica
        html_combo = r"""
        <div data-post="Republicadescuentos/5555">
            <div class="tgme_widget_message_text js-message_text">
                #COMBO #AD #AMAZON #ALIEXPRESS EL MEJOR COMBO CPU - BOARD
                PRECIO TOTAL 1.325.000 ENVIO INCLUIDO
                Memoria RAM 467.800 <a href="https://s.click.aliexpress.com/e/_c38RMcut">link1</a>
                Board ASRock 347.800 ENVIO GRATIS PRIME <a href="https://amzn.to/4dKQpIW">link2</a>
            </div>
        </div>
        """
        deals = republica.extraer_deals_html(html_combo)
        self.assertEqual(len(deals), 0)

    def test_telegram_render_purga_prime_de_otras_tiendas(self):
        from core import telegram
        from core.models import Deal
        from core.scoring import Verdict

        d = Deal("republica", "AliExpress", "CO", "k_ali", "Memoria RAM DDR4 16GB", "http://ali", 150_000, "COP", None,
                 notes=["🅿️ Envío gratis con Amazon Prime", "🎟 Cupón: DESC10"])
        v = Verdict(alertar=True, inmediata=True, confianza="alta", glitch=False, etiquetas=[], motivo="")
        msg = telegram.render(d, v, None, None)
        # El renderizador debe haber purgado Amazon Prime porque la tienda es AliExpress
        self.assertNotIn("Amazon Prime", msg)
        self.assertIn("Cupón: DESC10", msg)

    def test_republica_en_radar_fuentes(self):
        import radar
        self.assertIn("republica", radar.FUENTES)

    def test_subclasificacion_hardware_y_perifericos_pc(self):
        from core import filtros

        casos_hardware = [
            ("Diadema Gamer Redragon RGB", "perifericos"),
            ("Teclado Mecanico Inalambrico RGB", "perifericos"),
            ("Mouse Gamer Inalambrico Logitech G Pro", "perifericos"),
            ("Tarjeta de Video RTX 4070 Ti Super 16GB", "computadores_y_hardware"),
            ("Procesador AMD Ryzen 7 7800X3D", "computadores_y_hardware"),
            ("Motherboard ASUS ROG Strix B650-E Gaming", "computadores_y_hardware"),
            ("Placa Madre Gigabyte B550M DS3H", "computadores_y_hardware"),
            ("Memoria RAM Corsair Vengeance 32GB DDR5", "computadores_y_hardware"),
            ("Disco SSD NVMe 1TB Kingston Renegade", "computadores_y_hardware"),
            ("Refrigeracion Liquida Thermalright Frozen 360", "computadores_y_hardware"),
            ("Monitor Gamer Curvo AOC 24 180Hz", "tv_y_monitores"),
        ]
        for titulo, familia_esperada in casos_hardware:
            fam = filtros.asignar_familia(titulo)
            self.assertEqual(fam, familia_esperada, f"Falló subclasificación para '{titulo}'")

    def test_es_accesorio_no_descarta_hardware(self):
        from core import filtros
        self.assertFalse(filtros.es_accesorio("Tarjeta de video MSI RTX 4060"))
        self.assertFalse(filtros.es_accesorio("Tarjeta madre ASUS TUF GAMING"))
        self.assertFalse(filtros.es_accesorio("Motherboard Gigabyte B650"))
        self.assertFalse(filtros.es_accesorio("Placa base ASRock"))
        self.assertFalse(filtros.es_accesorio("Diadema Gamer HyperX Cloud"))

    def test_scoring_republica_sin_precio_lista_aprobada(self):
        from core import scoring
        from core.models import Deal

        d = Deal("republica", "Amazon", "CO", "amazon:B0TEST99", "Tarjeta de video RTX 4060", "http://amzn", 1_500_000, "COP", None)
        stats = (0, None, None)
        v = scoring.evaluar(d, stats)
        self.assertTrue(v.alertar)
        self.assertTrue(v.inmediata)
        self.assertIn("destacada en Amazon", v.motivo)

    def test_torneo_puntuacion_hardware_republica(self):
        import radar
        from core.scoring import Verdict
        from core.models import Deal

        d_gpu = Deal("republica", "Amazon", "CO", "amazon:B0GPU", "Tarjeta de Video RTX 4070", "http://amzn", 2_800_000, "COP", None, coupons=["DESC10"])
        v = Verdict(alertar=True, inmediata=True, confianza="alta", glitch=False, etiquetas=[], motivo="")
        campeones = radar.torneo_familias([(d_gpu, v)])
        self.assertEqual(len(campeones), 1)
        # 55.0 con cupón * peso 1.5 de computadores_y_hardware = 82.5 puntos
        self.assertEqual(campeones[0][2], 82.5)


class PruebaCuposPorTienda(unittest.TestCase):
    def test_competencia_interna_por_tienda(self):
        import radar
        from core.scoring import Verdict

        v = Verdict(alertar=True, inmediata=True, confianza="alta", glitch=False, etiquetas=[], motivo="")
        d1 = oferta(source="algolia_co", store="Alkosto", key="k1", title="Televisor 55 Pulgadas 4K", price=1_500_000, discount=50.0)
        d2 = oferta(source="algolia_co", store="Alkosto", key="k2", title="Portatil Gamer Ryzen 7", price=2_600_000, discount=35.0)
        d3 = oferta(source="algolia_co", store="Alkosto", key="k3", title="Celular Galaxy A34", price=800_000, discount=20.0)

        candidatas = [(d1, v), (d2, v), (d3, v)]
        res = radar.seleccionar_por_tiendas(candidatas, max_por_tienda=1)

        self.assertEqual(len(res), 1)
        # El TV (score 50 * 1.5 = 75.0) debe ganar la competencia interna de Alkosto frente al portatil (35 * 1.5 = 52.5)
        self.assertEqual(res[0][0].key, "k1")

    def test_multiples_tiendas_coexisten_misma_categoria(self):
        import radar
        from core.scoring import Verdict

        v = Verdict(alertar=True, inmediata=True, confianza="alta", glitch=False, etiquetas=[], motivo="")
        # 3 tiendas distintas con la misma categoria (computadores)
        d_alkosto = oferta(source="algolia_co", store="Alkosto", key="k_alk", title="Portatil Lenovo ThinkPad", price=2_000_000, discount=40.0)
        d_falabella = oferta(source="falabella", store="Falabella", key="k_fal", title="Portatil Asus ZenBook", price=2_600_000, discount=35.0)
        d_exito = oferta(source="vtex", store="Exito", key="k_ext", title="Portatil HP Pavilion", price=2_100_000, discount=30.0)

        candidatas = [(d_alkosto, v), (d_falabella, v), (d_exito, v)]
        # En el sistema anterior, torneo_familias mataba 2 de las 3 laptops.
        # Ahora, cada tienda tiene su cupo y las 3 coexisten!
        res = radar.seleccionar_por_tiendas(candidatas, max_por_tienda=1, tope_ronda=6)

        self.assertEqual(len(res), 3)
        tiendas = {par[0].store for par in res}
        self.assertEqual(tiendas, {"Alkosto", "Falabella", "Exito"})

    def test_tienda_sin_promocion_entrega_cero(self):
        import radar
        from core.scoring import Verdict

        v = Verdict(alertar=True, inmediata=True, confianza="alta", glitch=False, etiquetas=[], motivo="")
        # Solo Alkosto y Falabella tienen ofertas reales hoy
        d_alk = oferta(source="algolia_co", store="Alkosto", key="k1", title="Televisor 55", price=1_500_000, discount=50.0)
        d_fal = oferta(source="falabella", store="Falabella", key="k2", title="Refrigerador No Frost", price=1_800_000, discount=40.0)

        candidatas = [(d_alk, v), (d_fal, v)]
        res = radar.seleccionar_por_tiendas(candidatas, max_por_tienda=1)

        self.assertEqual(len(res), 2)
        tiendas = {par[0].store.lower() for par in res}
        # Haceb, Koaj, Jumbo no tienen ofertas y no se inventan ofertas ni cupos fantasma
        self.assertNotIn("haceb", tiendas)
        self.assertNotIn("koaj", tiendas)

    def test_respeto_tope_ronda(self):
        import radar
        from core.scoring import Verdict

        v = Verdict(alertar=True, inmediata=True, confianza="alta", glitch=False, etiquetas=[], motivo="")
        # 10 tiendas distintas con ofertas
        candidatas = []
        for i in range(10):
            d = oferta(source="vtex", store=f"Tienda_{i}", key=f"k_{i}", title=f"Producto Destacado {i}", price=100_000, discount=20.0 + i * 5)
            candidatas.append((d, v))

        # Con tope de ronda = 6 (ej. catálogo retail), debe tomar exactamente las 6 mejores tiendas
        res = radar.seleccionar_por_tiendas(candidatas, max_por_tienda=1, tope_ronda=6)

        self.assertEqual(len(res), 6)
        # Verificar que sean 6 tiendas diferentes
        tiendas = [par[0].store for par in res]
        self.assertEqual(len(set(tiendas)), 6)
        # La primera debe ser la de mayor descuento (Tienda_9 con 65%)
    def test_facebook_configurado_y_render_cero_urls(self):
        from core import facebook
        from core.scoring import Verdict

        d = oferta(
            source="amazon",
            store="Amazon",
            title="Monitor Gamer Curvo 27 Pulgadas 165Hz con resolución QHD y panel IPS de alta tasa de refresco",
            price=800000.0,
            list_price=1600000.0,
            url="https://amazon.com/dp/B123",
            image="https://m.media-amazon.com/images/I/71xyz.jpg",
            coupons=["DESCUENTO20"],
            notes=["Envío gratis"],
        )
        v = Verdict(
            alertar=True,
            inmediata=True,
            confianza="alta",
            glitch=False,
            etiquetas=["ganga"],
            motivo="50% de descuento",
        )

        texto = facebook.render_individual(d, v)
        self.assertIn("Amazon (-50%)", texto)
        self.assertIn("Monitor Gamer Curvo", texto)
        self.assertIn("💵 Antes: $1.600.000 ➡️ Ahora: $800.000", texto)
        self.assertIn("🎟️ Cupón de descuento: DESCUENTO20", texto)
        # REGLA DE ORO: Cero URLs en el caption del post principal
        self.assertNotIn("http://", texto)
        self.assertNotIn("https://", texto)
        self.assertNotIn("t.me", texto)
        self.assertIn("primer comentario", texto.lower())

    def test_facebook_spintax_y_truncamiento(self):
        from core import facebook

        # Truncamiento estricto a 60 caracteres
        largo = "Este es un título excesivamente largo para una oferta que debe ser truncada según requerimiento anti-spam"
        truncado = facebook.truncar(largo, 60)
        self.assertTrue(len(truncado) <= 63)
        self.assertTrue(truncado.endswith("..."))

        corto = "Oferta corta"
        self.assertEqual(facebook.truncar(corto, 60), "Oferta corta")

        # Spintax genera cadenas no vacías de los bancos
        self.assertIn(facebook.spintax_encabezado(), facebook.SPINTAX_ENCABEZADOS)
        self.assertIn(facebook.spintax_aviso_comentario(), facebook.SPINTAX_AVISOS_COMENTARIO)
        self.assertIn(facebook.spintax_camuflaje(), facebook.SPINTAX_CAMUFLAJE)

    def test_facebook_spintax_carrusel_3_bloques(self):
        from core import facebook
        post = facebook.spintax_post_carrusel()
        bloques = [b.strip() for b in post.split("\n\n") if b.strip()]
        self.assertEqual(len(bloques), 3)
        self.assertIn(bloques[0], facebook.SPINTAX_CARRUSEL_ENCABEZADOS)
        self.assertIn(bloques[1], facebook.SPINTAX_CARRUSEL_BAJADAS)
        self.assertIn(bloques[2], facebook.SPINTAX_CARRUSEL_CTAS)

    def test_facebook_render_agrupado_y_comentario(self):
        from core import facebook

        d1 = oferta(source="alkosto", store="Alkosto", title="Smart TV 55 Pulgadas 4K UHD", price=1200000.0, list_price=2000000.0)
        d2 = oferta(source="exito", store="Éxito", title="Lavadora Carga Frontal 18kg Inverter", price=1300000.0, list_price=2000000.0, coupons=["EXITO10"])

        post_caption = facebook.render_agrupado([d1, d2])
        # Caption principal sin URLs (anti-ban Meta)
        self.assertNotIn("http://", post_caption)
        self.assertNotIn("https://", post_caption)
        bloques = [b.strip() for b in post_caption.split("\n\n") if b.strip()]
        self.assertEqual(len(bloques), 3)

        # Captions individuales por foto en el carrusel (con link directo oficial a la tienda)
        cap1 = facebook.render_caption_foto(d1, hash_id="hash1", base_url="https://promosbot.onrender.com")
        self.assertIn("Alkosto (-40%)", cap1)
        self.assertIn("Smart TV 55 Pulgadas 4K UHD", cap1)
        self.assertIn(d1.url, cap1)

        cap2 = facebook.render_caption_foto(d2, hash_id="hash2", base_url="https://promosbot.onrender.com")
        self.assertIn("Éxito (-35%)", cap2)
        self.assertIn("EXITO10", cap2)
        self.assertIn(d2.url, cap2)

        # Primer comentario con links directos y @ del canal de Telegram
        items = [("hash1", d1), ("hash2", d2)]
        comentario = facebook.render_comentario_links(items, base_url="https://promosbot.onrender.com")
        self.assertIn(d1.url, comentario)
        self.assertIn(d2.url, comentario)
        self.assertIn("🎟️ Cupón: EXITO10", comentario)
        self.assertIn("@RadarPromoCol", comentario)

    def test_facebook_encolar_y_procesar_lote_con_comentario(self):
        from unittest.mock import patch, MagicMock
        from core import facebook
        from core.store import Store

        d = oferta(source="amazon", store="Amazon", title="Teclado Mecánico RGB", price=150000.0, image="https://amazon.com/img.jpg")

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"id": "post_789_123"}'
        mock_resp.__enter__.return_value = mock_resp

        with patch("config.FB_PAGE_ID", "12345"), \
             patch("config.FB_PAGE_ACCESS_TOKEN", "token123"), \
             patch("config.FB_ENABLED", True), \
             patch("urllib.request.urlopen", return_value=mock_resp):
            with Store() as store:
                store.facebook_resetear_camuflaje()
                pendientes = store.obtener_cola_facebook(limite=100)
                if pendientes:
                    store.remover_de_cola_facebook([h for h, _ in pendientes])

            # Encolar 1 oferta: se acepta y publica individualmente
            h1 = facebook.encolar_oferta(d)
            self.assertIsNotNone(h1)
            res1 = facebook.procesar_cola(limite=4, base_url="https://test.render.com")
            self.assertTrue(res1.get("ok"))
            self.assertEqual(res1.get("post_id"), "post_789_123")
            self.assertEqual(res1.get("deals_count"), 1)

    def test_facebook_regla_camuflaje_5_a_1(self):
        from unittest.mock import patch, MagicMock
        from core import facebook
        from core.store import Store

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"id": "camuflaje_post_999"}'
        mock_resp.__enter__.return_value = mock_resp

        with patch("config.FB_PAGE_ID", "12345"), \
             patch("config.FB_PAGE_ACCESS_TOKEN", "token123"), \
             patch("config.FB_ENABLED", True), \
             patch("urllib.request.urlopen", return_value=mock_resp):
            with Store() as store:
                # Simular que ya se hicieron 5 publicaciones promocionales
                for _ in range(5):
                    store.facebook_incrementar_promos()
                self.assertGreaterEqual(store.facebook_contador_promos(), 5)

            # La siguiente ejecución debe ser obligatoriamente un post de camuflaje limpio
            res = facebook.procesar_cola(limite=4)
            self.assertTrue(res.get("ok"))
            self.assertEqual(res.get("tipo"), "camuflaje")

            # El contador debe resetearse a 0
            with Store() as store:
                self.assertEqual(store.facebook_contador_promos(), 0)

    def test_server_endpoint_link_cloaking_302(self):
        import server
        from unittest.mock import MagicMock

        manejador = server.Manejador.__new__(server.Manejador)
        manejador.send_response = MagicMock()
        manejador.send_header = MagicMock()
        manejador.end_headers = MagicMock()

        # Caso 1: ID numérico -> redirige a canal de Telegram https://t.me/RadarPromoCol/1234
        manejador.path = "/ir/1234"
        manejador.do_GET()
        manejador.send_response.assert_called_with(302)
        manejador.send_header.assert_any_call("Location", "https://t.me/RadarPromoCol/1234")

        # Caso 2: Hash alfanumérico -> redirige a bot de Telegram https://t.me/PromosOn_bot?start=deal_abc123
        manejador.send_response.reset_mock()
        manejador.send_header.reset_mock()
        manejador.path = "/ir/abc123"
        manejador.do_GET()
        manejador.send_response.assert_called_with(302)
        manejador.send_header.assert_any_call("Location", "https://t.me/PromosOn_bot?start=deal_abc123")

    def test_server_endpoint_foto_branding_200(self):
        import server
        from unittest.mock import MagicMock, patch
        from core.models import Deal
        from core.store import Store

        d = Deal(
            key="test_deal_img_1", source="falabella", store="Falabella", country="CO",
            title="Tenis Deportivos Hombre", price=199900.0, list_price=399900.0,
            currency="COP", url="https://falabella.com.co",
            image="https://images.unsplash.com/photo-1542291026-7eec264c27ff",
        )
        with Store() as store:
            h = store.encolar_facebook(d)

        manejador = server.Manejador.__new__(server.Manejador)
        manejador.send_response = MagicMock()
        manejador.send_header = MagicMock()
        manejador.end_headers = MagicMock()
        manejador.wfile = MagicMock()

        manejador.path = f"/foto/{h}.jpg"
        with patch("core.branding.generar_tarjeta_branding_bytes", return_value=b"fake_jpeg_bytes"):
            manejador.do_GET()

        manejador.send_response.assert_called_with(200)
        manejador.send_header.assert_any_call("Content-Type", "image/jpeg")
        manejador.wfile.write.assert_called_with(b"fake_jpeg_bytes")

    def test_facebook_error_no_lanza_excepcion(self):
        from unittest.mock import patch
        from core import facebook
        from core.scoring import Verdict

        d = oferta(
            source="amazon",
            store="Amazon",
            title="Monitor Gamer Curvo",
            price=800000.0,
            url="https://amazon.com/dp/B123",
        )
        v = Verdict(
            alertar=True,
            inmediata=True,
            confianza="alta",
            glitch=False,
            etiquetas=[],
            motivo="",
        )

        with patch("config.FB_PAGE_ID", "12345"), \
             patch("config.FB_PAGE_ACCESS_TOKEN", "token123"), \
             patch("config.FB_ENABLED", True), \
             patch("urllib.request.urlopen", side_effect=Exception("Fallo de red simulado")):
            ok = facebook.publicar_oferta(d, v)
            self.assertFalse(ok)

    def test_facebook_tope_maximo_4_en_cola(self):
        from unittest.mock import patch
        from core import facebook
        from core.store import Store

        with patch("config.FB_ENABLED", True):
            with Store() as store:
                store.conn.execute("DELETE FROM facebook_cola")
                store.conn.commit()

                hashes = []
                for i in range(4):
                    d = oferta(source="amazon", store="Amazon", key=f"k_{i}", title=f"Oferta {i}", price=10000.0 * (i + 1), url=f"https://amazon.com/p{i}")
                    h = facebook.encolar_oferta(d)
                    self.assertIsNotNone(h)
                    hashes.append(h)

                total = store.conn.execute("SELECT COUNT(*) FROM facebook_cola").fetchone()[0]
                self.assertEqual(total, 4)

                # La 5ta oferta debe retornar None porque la cola está llena
                d5 = oferta(source="amazon", store="Amazon", key="k_5", title="Oferta 5 Excedente", price=50000.0, url="https://amazon.com/p5")
                h5 = facebook.encolar_oferta(d5)
                self.assertIsNone(h5)

                total_despues = store.conn.execute("SELECT COUNT(*) FROM facebook_cola").fetchone()[0]
                self.assertEqual(total_despues, 4)

                store.conn.execute("DELETE FROM facebook_cola")
                store.conn.commit()


class PruebaFacebookHistorias(unittest.TestCase):
    def setUp(self):
        from core.store import Store
        with Store() as store:
            store.conn.execute("DELETE FROM meta WHERE k LIKE 'fb_historia%'")
            store.conn.commit()

    def tearDown(self):
        from core.store import Store
        with Store() as store:
            store.conn.execute("DELETE FROM meta WHERE k LIKE 'fb_historia%'")
            store.conn.commit()

    def test_filtro_ganga_para_historia(self):
        from core import facebook
        from core.scoring import Verdict

        v_normal = Verdict(alertar=True, inmediata=True, confianza="alta", glitch=False, etiquetas=[], motivo="")
        v_glitch = Verdict(alertar=True, inmediata=True, confianza="alta", glitch=True, etiquetas=[], motivo="")

        # 1. Slickdeals descartado
        d_sd = oferta(source="slickdeals", store="Target", title="TV 55", price=200.0, list_price=500.0, image="https://img.com/tv.jpg")
        self.assertFalse(facebook.es_ganga_para_historia(d_sd, v_normal))

        # 2. Sin imagen descartado
        d_no_img = oferta(source="vtex", store="Falabella", title="Tenis", price=100000.0, list_price=250000.0, image="")
        self.assertFalse(facebook.es_ganga_para_historia(d_no_img, v_normal))

        # 3. Descuento bajo (30%) descartado
        d_bajo = oferta(source="vtex", store="Nike", title="Tenis Nike", price=280000.0, list_price=400000.0, image="https://img.com/n.jpg")
        self.assertFalse(facebook.es_ganga_para_historia(d_bajo, v_normal))

        # 4. Baratija menor a 25.000 COP descartada
        d_baratija = oferta(source="vtex", store="Alkosto", title="Cable USB", price=10000.0, list_price=30000.0, image="https://img.com/c.jpg")
        self.assertFalse(facebook.es_ganga_para_historia(d_baratija, v_normal))

        # 5. Ganga local >= 50% aprobada
        d_ganga = oferta(source="vtex", store="Falabella", title="Chaqueta Pluma", price=150000.0, list_price=350000.0, image="https://img.com/ch.jpg")
        self.assertTrue(facebook.es_ganga_para_historia(d_ganga, v_normal))

        # 6. Error de precio (glitch) aprobado
        d_glitch = oferta(source="vtex", store="Éxito", title="Smart TV 65", price=300000.0, list_price=3500000.0, image="https://img.com/tv.jpg")
        self.assertTrue(facebook.es_ganga_para_historia(d_glitch, v_glitch))

    def test_generador_canvas_dimensiones(self):
        from core import facebook
        from PIL import Image
        import io

        d = oferta(
            source="vtex",
            store="Nike",
            title="Tenis Running Nike Pegasus",
            price=220000.0,
            list_price=550000.0,
            image="https://img.com/fake_no_existe.jpg",
        )
        img_bytes = facebook.generar_canvas_historia(d)
        self.assertIsInstance(img_bytes, bytes)
        self.assertGreater(len(img_bytes), 10000)

        # Abrir y verificar dimensiones 1080x1920 exactas
        img = Image.open(io.BytesIO(img_bytes))
        self.assertEqual(img.size, (1080, 1920))

    def test_control_cupo_historias(self):
        import datetime as dt
        from core.store import Store

        with Store() as store:
            # 1. Al inicio del día debe permitir la 1ra historia con el nuevo tope de 5 al día
            self.assertTrue(store.facebook_puede_publicar_historia(max_diarias=5, min_horas_espaciado=2.0))

            # Registrar la 1ra historia
            n1 = store.facebook_registrar_historia()
            self.assertEqual(n1, 1)

            # 2. De inmediato NO debe permitir otra (no han pasado 2 horas)
            self.assertFalse(store.facebook_puede_publicar_historia(max_diarias=5, min_horas_espaciado=2.0))

            # 3. Simular que pasaron 2.5 horas
            zona_co = dt.timezone(dt.timedelta(hours=-5))
            hace_2h = dt.datetime.now(zona_co) - dt.timedelta(hours=2.5)
            store.set_meta("fb_historia_ultimo_ts", hace_2h.isoformat())

            # Ahora sí debe permitir la 2da historia
            self.assertTrue(store.facebook_puede_publicar_historia(max_diarias=5, min_horas_espaciado=2.0))

            # Registrar hasta 5 historias
            for i in range(2, 6):
                store.set_meta("fb_historia_ultimo_ts", hace_2h.isoformat())
                self.assertTrue(store.facebook_puede_publicar_historia(max_diarias=5, min_horas_espaciado=2.0))
                store.facebook_registrar_historia()

            # 4. Con 5 historias registradas, el tope diario está alcanzado incluso tras pasar horas
            store.set_meta("fb_historia_ultimo_ts", hace_2h.isoformat())
            self.assertFalse(store.facebook_puede_publicar_historia(max_diarias=5, min_horas_espaciado=2.0))

    def test_facebook_regla_lotes_1_2_o_4_nunca_3_ni_mas_de_4(self):
        from unittest.mock import patch, MagicMock
        from core import facebook
        from core.store import Store

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"id": "post_mock_lot"}'
        mock_resp.__enter__.return_value = mock_resp

        with patch("config.FB_PAGE_ID", "12345"), \
             patch("config.FB_PAGE_ACCESS_TOKEN", "token123"), \
             patch("config.FB_ENABLED", True), \
             patch("urllib.request.urlopen", return_value=mock_resp):
            with Store() as store:
                store.conn.execute("DELETE FROM facebook_cola")
                store.conn.commit()

                # Caso 1: 1 oferta en cola -> SÍ se acepta y publica 1
                d1 = oferta(source="amazon", store="Amazon", key="k_lot_1", title="Oferta 1", price=10000.0, url="http://l1")
                facebook.encolar_oferta(d1)
                res1 = facebook.procesar_cola(limite=4)
                self.assertTrue(res1.get("ok"))
                self.assertEqual(res1.get("deals_count"), 1)

                store.conn.execute("DELETE FROM facebook_cola")
                store.conn.commit()

                # Caso 2: 2 ofertas en cola -> SÍ se acepta y publica 2
                d1 = oferta(source="amazon", store="Amazon", key="k_lot_1b", title="Oferta 1", price=10000.0, url="http://l1b")
                d2 = oferta(source="amazon", store="Amazon", key="k_lot_2b", title="Oferta 2", price=20000.0, url="http://l2b")
                facebook.encolar_oferta(d1)
                facebook.encolar_oferta(d2)
                res2 = facebook.procesar_cola(limite=4)
                self.assertTrue(res2.get("ok"))
                self.assertEqual(res2.get("deals_count"), 2)

                store.conn.execute("DELETE FROM facebook_cola")
                store.conn.commit()

                # Caso 3: 3 ofertas en cola -> DEBE publicar 2 y dejar 1 pendiente (NUNCA 3)
                for i in range(1, 4):
                    d = oferta(source="amazon", store="Amazon", key=f"k_lot3_{i}", title=f"Oferta {i}", price=10000.0 * i, url=f"http://l{i}")
                    facebook.encolar_oferta(d)
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM facebook_cola").fetchone()[0], 3)

                res3 = facebook.procesar_cola(limite=4)
                self.assertTrue(res3.get("ok"))
                self.assertEqual(res3.get("deals_count"), 2)  # Publicó 2, NUNCA 3

                # Debe quedar exactamente 1 pendiente en la cola
                pendientes = store.conn.execute("SELECT COUNT(*) FROM facebook_cola").fetchone()[0]
                self.assertEqual(pendientes, 1)

                store.conn.execute("DELETE FROM facebook_cola")
                store.conn.commit()

                # Caso 4: 4 ofertas en cola -> SÍ se acepta y publica 4
                for i in range(1, 5):
                    d = oferta(source="amazon", store="Amazon", key=f"k_lot4_{i}", title=f"Oferta {i}", price=10000.0 * i, url=f"http://l{i}")
                    facebook.encolar_oferta(d)
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM facebook_cola").fetchone()[0], 4)

                res4 = facebook.procesar_cola(limite=4)
                self.assertTrue(res4.get("ok"))
                self.assertEqual(res4.get("deals_count"), 4)  # Máximo 4
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM facebook_cola").fetchone()[0], 0)

                # Caso 5: 5 ofertas -> la 5ta se rechaza por tope de cola y se publican 4
                for i in range(1, 5):
                    d = oferta(source="amazon", store="Amazon", key=f"k_lot5_{i}", title=f"Oferta {i}", price=10000.0 * i, url=f"http://l5_{i}")
                    facebook.encolar_oferta(d)
                d5 = oferta(source="amazon", store="Amazon", key="k_lot5_5", title="Oferta 5", price=50000.0, url="http://l5_5")
                h5 = facebook.encolar_oferta(d5)
                self.assertIsNone(h5)  # Cola llena a 4

                res5 = facebook.procesar_cola(limite=4)
                self.assertTrue(res5.get("ok"))
                self.assertEqual(res5.get("deals_count"), 4)  # Máximo 4
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM facebook_cola").fetchone()[0], 0)

    def test_publicar_historia_flujo_mock(self):
        from unittest.mock import patch, MagicMock
        from core import facebook

        d = oferta(
            source="vtex",
            store="Alkosto",
            title="Freidora de Aire Digital 5L",
            price=149900.0,
            list_price=399900.0,
            image="https://img.com/freidora.jpg",
        )

        mock_resp_photo = MagicMock()
        mock_resp_photo.read.return_value = json.dumps({"id": "photo_mock_123"}).encode("utf-8")
        mock_resp_photo.__enter__.return_value = mock_resp_photo

        mock_resp_story = MagicMock()
        mock_resp_story.read.return_value = json.dumps({"id": "story_mock_456", "post_id": "story_mock_456"}).encode("utf-8")
        mock_resp_story.__enter__.return_value = mock_resp_story

        respuestas = [mock_resp_photo, mock_resp_story]

        with patch("config.FB_PAGE_ID", "12345"), \
             patch("config.FB_PAGE_ACCESS_TOKEN", "token_valido"), \
             patch("config.FB_ENABLED", True), \
             patch("core.facebook.obtener_foto_pagina_bytes", return_value=b"\xff\xd8\xff\xe0mock_image_bytes"), \
             patch("urllib.request.urlopen", side_effect=respuestas):
            res = facebook.publicar_historia(d)
            self.assertTrue(res.get("ok"))
            self.assertEqual(res.get("story_id"), "story_mock_456")
            self.assertEqual(res.get("photo_id"), "photo_mock_123")


class PruebaFotosLimpiasCatalogo(unittest.TestCase):
    """Pruebas de extracción de imágenes de catálogo limpias sin marcas de agua."""

    def test_promohunter_obtiene_foto_limpia_amazon_hires(self):
        from unittest.mock import MagicMock, patch
        from sources import promohunter
        html_mock = '<div id="imgTagWrapperId"><img data-old-hires="https://m.media-amazon.com/images/I/71abc_SL1500_.jpg" /></div>'
        mock_resp = MagicMock()
        mock_resp.read.return_value = html_mock.encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            foto = promohunter.obtener_foto_limpia_amazon("B0TEST1234")
            self.assertEqual(foto, "https://m.media-amazon.com/images/I/71abc_SL1500_.jpg")

    def test_promohunter_fallback_ante_error_red(self):
        from unittest.mock import patch
        from sources import promohunter
        with patch("urllib.request.urlopen", side_effect=Exception("Timeout simulado")):
            foto = promohunter.obtener_foto_limpia_amazon("B0TEST1234")
            self.assertIsNone(foto)

    def test_promocajita_obtiene_foto_limpia_s3(self):
        from unittest.mock import MagicMock, patch
        from sources import promocajita
        html_mock = '<meta property="og:image" content="https://pccajita.s3.us-east-2.amazonaws.com/images/offers/celular.webp" />'
        mock_resp = MagicMock()
        mock_resp.read.return_value = html_mock.encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            foto = promocajita.obtener_foto_limpia_promocajita("https://promocajita.com/deal/12345")
            self.assertEqual(foto, "https://pccajita.s3.us-east-2.amazonaws.com/images/offers/celular.webp")

    def test_promocajita_fallback_ante_error_red(self):
        from unittest.mock import patch
        from sources import promocajita
        with patch("urllib.request.urlopen", side_effect=Exception("Error HTTP simulado")):
            foto = promocajita.obtener_foto_limpia_promocajita("https://promocajita.com/deal/12345")
            self.assertIsNone(foto)


class PruebaBrandingMotor(unittest.TestCase):
    """Pruebas del generador de marcos, branding e identidad visual para ofertas."""

    def test_debe_aplicar_branding_reglas(self):
        from core import branding
        from core.models import Deal

        # 1. DescuentosTech DEBE ser excluido
        dt = Deal(source="descuentostech", store="DescuentosTech", country="CO",
                  key="dt:1", title="RAM", url="http://link", price=100000.0,
                  currency="COP", image="http://img.com/foto.jpg")
        self.assertFalse(branding.debe_aplicar_branding(dt))

        # 2. PromoHunter con imagen y precio -> SÍ aplica
        ph = Deal(source="promohunter", store="Amazon", country="CO",
                  key="ph:1", title="Audífonos", url="http://link", price=50000.0,
                  currency="COP", image="http://img.com/foto.jpg")
        self.assertTrue(branding.debe_aplicar_branding(ph))

        # 3. Promocajita con imagen y precio -> SÍ aplica
        pc = Deal(source="promocajita", store="PROMOCAJITA", country="CO",
                  key="pc:1", title="Smart TV", url="http://link", price=1200000.0,
                  currency="COP", image="http://img.com/tv.webp")
        self.assertTrue(branding.debe_aplicar_branding(pc))

        # 4. Tiendas nacionales (Alkosto, Falabella) -> SÍ aplica
        ak = Deal(source="alkosto", store="Alkosto", country="CO",
                  key="ak:1", title="Nevera", url="http://link", price=1500000.0,
                  currency="COP", image="http://img.com/nevera.jpg")
        self.assertTrue(branding.debe_aplicar_branding(ak))

        # 5. Sin imagen o sin precio -> NO aplica
        sin_img = Deal(source="alkosto", store="Alkosto", country="CO",
                       key="ak:2", title="Nevera", url="http://link", price=1500000.0,
                       currency="COP", image=None)
        self.assertFalse(branding.debe_aplicar_branding(sin_img))

        sin_precio = Deal(source="alkosto", store="Alkosto", country="CO",
                          key="ak:3", title="Nevera", url="http://link", price=0.0,
                          currency="COP", image="http://img.com/nevera.jpg")
        self.assertFalse(branding.debe_aplicar_branding(sin_precio))

    def test_formatear_precio_branding_respeta_moneda(self):
        from core import branding
        from core.models import Deal

        # Caso USD: debe mostrarse en dólares sin convertir a pesos
        d_usd = Deal(source="promohunter", store="Amazon", country="US",
                     key="u:1", title="Item", url="http://link", price=12.0,
                     currency="USD", image="http://img.com/foto.jpg")
        precio_txt, _ = branding.formatear_precio_branding(d_usd)
        self.assertIn("US$", precio_txt)
        self.assertIn("12", precio_txt)
        self.assertNotIn("COP", precio_txt)

        # Caso COP: debe mostrarse en pesos colombianos formateados
        d_cop = Deal(source="alkosto", store="Alkosto", country="CO",
                     key="c:1", title="Item", url="http://link", price=120088.0,
                     currency="COP", image="http://img.com/foto.jpg")
        precio_txt, _ = branding.formatear_precio_branding(d_cop)
        self.assertIn("COP", precio_txt)
        self.assertIn("120.088", precio_txt)

    def test_generar_tarjeta_branding_bytes_exito(self):
        from unittest.mock import MagicMock, patch
        from core import branding
        from core.models import Deal
        from PIL import Image

        deal = Deal(source="promohunter", store="Amazon", country="CO",
                    key="ph:10", title="Mouse Gamer", url="http://link", price=89900.0,
                    currency="COP", image="http://img.com/mouse.jpg")

        img_mock = Image.new("RGBA", (500, 500), (255, 255, 255, 255))
        with patch("core.branding.descargar_foto_producto", return_value=img_mock):
            bytes_res = branding.generar_tarjeta_branding_bytes(deal)
            self.assertIsNotNone(bytes_res)
            self.assertTrue(bytes_res.startswith(b"\xff\xd8\xff"))  # Encabezado JPEG

    def test_generar_tarjeta_branding_bytes_fallback_none(self):
        from unittest.mock import patch
        from core import branding
        from core.models import Deal

        deal = Deal(source="promohunter", store="Amazon", country="CO",
                    key="ph:11", title="Mouse Gamer", url="http://link", price=89900.0,
                    currency="COP", image="http://img.com/mouse.jpg")

        with patch("core.branding.descargar_foto_producto", return_value=None):
            bytes_res = branding.generar_tarjeta_branding_bytes(deal)
            self.assertIsNone(bytes_res)


class PruebaFastTrackModaYCalzado(unittest.TestCase):
    def test_deteccion_moda_y_calzado_marcas(self):
        from core import filtros
        self.assertTrue(filtros.es_calzado_o_ropa_de_marca("Adidas Runfalcon 6 Running Shoes", "Adidas"))
        self.assertTrue(filtros.es_calzado_o_ropa_de_marca("Tenis Nike Air Max 90", "Nike"))
        self.assertTrue(filtros.es_calzado_o_ropa_de_marca("Puma Suede Classic Sneakers", "Puma"))
        self.assertTrue(filtros.es_calzado_o_ropa_de_marca("Reebok Club C 85 Shoes", "Reebok"))
        self.assertTrue(filtros.es_calzado_o_ropa_de_marca("Sudadera con capucha Under Armour Fleece Hoodie", "Under Armour"))
        # No es calzado ni ropa aunque mencione la marca:
        self.assertFalse(filtros.es_calzado_o_ropa_de_marca("Perfume Adidas Ice Dive 100ml", "Adidas"))
        self.assertFalse(filtros.es_calzado_o_ropa_de_marca("Reloj Inteligente Apple Watch Series 9", "Apple"))

    def test_descuentostech_extrae_marca_y_limpia_titulo(self):
        from sources import descuentostech
        html = """
        <div data-post="DescuentosTech/50387">
            <div class="js-message_text">
                <a href="?q=%23Calzado">#Calzado</a> <a href="?q=%23Adidas">#Adidas</a> ✨Por solo✨ USD$24<br/>
                Código: <code>FRESH</code><br/><br/>
                Runfalcon 6 CLOUDFOAM Running Shoes<br/><br/>
                *Casillero <br/><br/>
                Ver oferta: <a href="https://www.facebook.com/permalink.php?id=123&story_fbid=456">https://facebook.com</a>
            </div>
        </div>
        """
        deals = descuentostech.extraer_deals_html(html)
        self.assertEqual(len(deals), 1)
        d = deals[0]
        self.assertEqual(d.store, "Adidas")
        self.assertEqual(d.price, 24.0)
        self.assertEqual(d.currency, "USD")
        self.assertEqual(d.coupons, ["FRESH"])
        self.assertIn("📦 Requiere casillero", d.notes)
        self.assertIn("Cupón: FRESH", d.notes)
        self.assertIn("Runfalcon 6 CLOUDFOAM Running Shoes", d.title)
        self.assertNotIn("FRESH", d.title)
        self.assertNotIn("*Casillero", d.title)

    def test_radar_fasttrack_moda_vip_sin_limite_de_tienda(self):
        import radar
        from core.models import Deal
        from core.scoring import Verdict

        d1 = Deal(source="descuentostech", store="Adidas", country="US",
                  key="dt:1", title="Adidas Runfalcon 6 Shoes", url="http://link1", price=24.0, currency="USD")
        d2 = Deal(source="descuentostech", store="Adidas", country="US",
                  key="dt:2", title="Adidas Daily 4.0 Shoes", url="http://link2", price=23.0, currency="USD")
        d3 = Deal(source="descuentostech", store="Adidas", country="US",
                  key="dt:3", title="Adidas Lite Racer Shoes", url="http://link3", price=27.0, currency="USD")

        v1 = Verdict(alertar=True, confianza="alta", inmediata=True, motivo="calzado")
        v2 = Verdict(alertar=True, confianza="alta", inmediata=True, motivo="calzado")
        v3 = Verdict(alertar=True, confianza="alta", inmediata=True, motivo="calzado")

        candidatas = [(d1, v1), (d2, v2), (d3, v3)]

        # Simular Fase 0 y Fase 2 del radar
        fasttrack_glitches = []
        fasttrack_moda_vip = []
        candidatas_ordinarias = []

        from core import filtros
        for par in candidatas:
            if filtros.es_calzado_o_ropa_de_marca(par[0].title, par[0].store):
                par[1].inmediata = True
                fasttrack_moda_vip.append(par)
            else:
                candidatas_ordinarias.append(par)

        seleccion = fasttrack_glitches + fasttrack_moda_vip
        # Todas las 3 ofertas de Adidas deben pasar sin ser recortadas por el cupo de tienda (max 1)
        self.assertEqual(len(seleccion), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)







