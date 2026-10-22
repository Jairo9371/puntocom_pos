'use strict';

// DOM mínimo para ejecutar el script Jinja renderizado, no una copia de su lógica.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');
const fixtures = JSON.parse(fs.readFileSync(0, 'utf8'));
const storageKey = 'puntocom-pos:1';

function locksCompartidos() {
  const propietarios = new Map();
  return {
    para(owner) {
      return {
        request(name, options, callback) {
          if (typeof options === 'function') callback = options;
          if (propietarios.has(name)) return Promise.resolve(callback(null));
          propietarios.set(name, owner);
          return Promise.resolve(callback({ name }));
        }
      };
    },
    cerrar(owner) {
      for (const [name, current] of propietarios) {
        if (current === owner) propietarios.delete(name);
      }
    }
  };
}

function respuesta(data, status = 200, tipo = 'application/json') {
  return { ok: status >= 200 && status < 300, status, redirected: false,
    headers: { get: () => tipo }, json: async () => data };
}

function pagina({ abierto = true, storage = new Map(), locks = locksCompartidos(), fetch, productos } = {}) {
  const fixture = fixtures[abierto ? 'abierto' : 'cerrado'];
  const elementos = new Map();
  for (const match of fixture.html.matchAll(/<([\w-]+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
    const attrs = match[2];
    const clases = new Set((/class="([^"]*)"/.exec(attrs)?.[1] || '').split(/\s+/));
    elementos.set(match[3], {
      value: '', textContent: '', innerHTML: '', disabled: /\bdisabled\b/.test(attrs),
      style: {}, dataset: {}, handlers: {}, focus() {},
      addEventListener(type, callback) { this.handlers[type] = callback; },
      classList: {
        toggle(name, enabled) { if (enabled) clases.add(name); else clases.delete(name); },
        contains(name) { return clases.has(name); }
      }
    });
  }
  const el = id => {
    assert.ok(elementos.has(id), `Elemento inexistente: ${id}`);
    return elementos.get(id);
  };
  el('pos-productos').textContent = productos === undefined ? fixture.productos : JSON.stringify(productos);
  el('pos-config').dataset = {
    csrfToken: 'csrf-de-prueba', cobrarUrl: '/cobrar', usuario: '1', turnoAbierto: String(abierto)
  };
  el('cliente').value = '1';
  el('metodo').value = 'EFECTIVO';
  el('estado-pago').value = 'PENDIENTE';
  const owner = {};
  const peticiones = [];
  const sandbox = {
    document: { getElementById: el }, crypto: webcrypto, Uint8Array,
    navigator: { locks: locks.para(owner) },
    sessionStorage: {
      getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value)
    },
    bootstrap: { Modal: class { show() {} hide() {} } },
    window: { location: { href: '' } }, alert() {}, confirm: () => true,
    setTimeout, clearTimeout,
    fetch: async (url, options) => {
      peticiones.push({ url, ...options });
      return fetch ? fetch(url, options) : respuesta({ ok: true, id_venta: 91 });
    }
  };
  const context = vm.createContext(sandbox);
  vm.runInContext(fixture.script, context, { filename: 'pos-renderizado.js' });
  return {
    el, peticiones, storage,
    ejecutar: script => vm.runInContext(script, context),
    estado: () => JSON.parse(vm.runInContext('JSON.stringify({items,intento,claveActual,posListo,productos})', context)),
    guardado: () => JSON.parse(storage.get(storageKey)),
    cerrar: () => locks.cerrar(owner),
    destino: () => sandbox.window.location.href
  };
}

function preparar(page) {
  assert.equal(page.estado().posListo, true);
  page.ejecutar('agregar(1); abrirCobro();');
  assert.equal(page.el('recibido').value, 10);
}

async function intentoSinRespuesta(options = {}) {
  const page = pagina({ ...options, fetch: async () => { throw new Error('Respuesta perdida'); } });
  preparar(page);
  await page.ejecutar('confirmarCobro()');
  assert.ok(page.guardado().intento);
  return page;
}

const casos = {
  async segunda_venta() {
    for (const metodo of ['EFECTIVO', 'TRANSFERENCIA']) {
      const storage = new Map();
      const locks = locksCompartidos();
      let siguienteVenta = 30;
      const backend = async () => respuesta({ ok: true, id_venta: ++siguienteVenta });
      const primera = pagina({ storage, locks, fetch: backend });
      preparar(primera);
      primera.el('cliente').value = '2';
      primera.el('metodo').value = metodo;
      primera.el('estado-pago').value = 'CONFIRMADO';
      primera.el('recibido').value = '100';
      primera.ejecutar('pagoCambia()');
      await primera.ejecutar('confirmarCobro()');
      assert.equal(primera.destino(), '/ventas/31/ticket');
      primera.cerrar();

      // Volver desde el ticket conserva la sesión del navegador, pero inicia otra venta.
      const segunda = pagina({ storage, locks, fetch: backend });
      assert.equal(segunda.el('cliente').value, '1', 'La venta nueva usa Consumidor Final');
      assert.equal(segunda.el('metodo').value, 'EFECTIVO', 'El método anterior no pasa a la venta nueva');
      assert.equal(segunda.el('estado-pago').value, 'PENDIENTE');
      segunda.ejecutar('agregar(1); agregar(1); abrirCobro();');
      assert.equal(segunda.el('modal-total').textContent, 'Q 20.00');
      assert.equal(Number(segunda.el('recibido').value), 20, 'El importe sugerido corresponde a la segunda venta');
      assert.equal(segunda.el('cambio').textContent, 'Q 0.00');
      for (const id of ['cliente', 'metodo', 'estado-pago', 'recibido']) {
        assert.equal(segunda.el(id).disabled, false);
      }
      await segunda.ejecutar('confirmarCobro()');
      assert.equal(segunda.destino(), '/ventas/32/ticket');
      assert.notEqual(segunda.peticiones[0].headers['Idempotency-Key'], primera.peticiones[0].headers['Idempotency-Key']);
      assert.notEqual(segunda.peticiones[0].body, primera.peticiones[0].body);
      const payload = JSON.parse(segunda.peticiones[0].body);
      assert.equal(payload.items[0].cantidad, 2);
      assert.equal(Number(payload.recibido), 20);
      assert.equal(payload.cliente_id, '1');
      assert.equal(payload.metodo, 'EFECTIVO');
      assert.equal(payload.estado_pago, 'CONFIRMADO');
      segunda.cerrar();
    }
  },

  async carrito_modificado() {
    const page = pagina();
    preparar(page);
    page.el('recibido').value = '100';
    page.ejecutar('actualizarCambio()');
    assert.equal(page.el('cambio').textContent, 'Q 90.00');
    // Cerrar el modal no crea un intento: se puede cambiar la cantidad y abrirlo otra vez.
    page.ejecutar('modal.hide(); cambiar(1, "cantidad", 2); abrirCobro();');
    assert.equal(page.el('modal-total').textContent, 'Q 20.00');
    assert.equal(Number(page.el('recibido').value), 20);
    assert.equal(page.el('cambio').textContent, 'Q 0.00');
    page.ejecutar('modal.hide(); cambiar(1, "descuento", 5); abrirCobro();');
    assert.equal(page.el('modal-total').textContent, 'Q 15.00');
    assert.equal(Number(page.el('recibido').value), 15);
    assert.equal(page.el('cambio').textContent, 'Q 0.00');
    assert.equal(page.peticiones.length, 0);
  },

  async reabrir_mismo_carrito() {
    const page = pagina();
    preparar(page);
    page.el('recibido').value = '100';
    page.ejecutar('actualizarCambio(); modal.hide(); abrirCobro();');
    assert.equal(Number(page.el('recibido').value), 100);
    assert.equal(page.el('cambio').textContent, 'Q 90.00');
    assert.equal(page.peticiones.length, 0);
  },

  async datos_venta_anterior() {
    const storage = new Map([[storageKey, JSON.stringify({
      tab: 'tab-antes-de-la-correccion', items: {}, intento: null, clave: null,
      catalogo: [], cliente: '2', metodo: 'TRANSFERENCIA', recibido: '100', pago: 'CONFIRMADO'
    })]]);
    const page = pagina({ storage });
    assert.equal(page.el('cliente').value, '1');
    assert.equal(page.el('metodo').value, 'EFECTIVO');
    assert.equal(page.el('estado-pago').value, 'PENDIENTE');
    page.ejecutar('agregar(1); agregar(1); abrirCobro();');
    assert.equal(page.el('modal-total').textContent, 'Q 20.00');
    assert.equal(Number(page.el('recibido').value), 20);
    assert.equal(page.el('cambio').textContent, 'Q 0.00');
  },

  async regreso_ticket() {
    const page = pagina();
    preparar(page);
    page.el('recibido').value = '100';
    page.ejecutar('actualizarCambio()');
    await page.ejecutar('confirmarCobro()');
    // Atrás puede recuperar el mismo DOM y JS de la caché del navegador.
    assert.deepEqual(page.estado().items, {});
    assert.equal(page.el('total').textContent, 'Q 0.00');
    assert.doesNotMatch(page.el('carrito').innerHTML, /Cuaderno/);
    assert.equal(page.el('cliente').disabled, false);
    page.ejecutar('agregar(1); agregar(1); abrirCobro();');
    assert.equal(page.el('modal-total').textContent, 'Q 20.00');
    assert.equal(Number(page.el('recibido').value), 20);
    assert.equal(page.el('cambio').textContent, 'Q 0.00');
    await page.ejecutar('confirmarCobro()');
    assert.notEqual(page.peticiones[0].headers['Idempotency-Key'], page.peticiones[1].headers['Idempotency-Key']);
    assert.equal(JSON.parse(page.peticiones[1].body).items[0].cantidad, 2);
  },

  async respuesta_perdida() {
    const storage = new Map();
    const locks = locksCompartidos();
    const ventas = new Map();
    let commits = 0;
    const backend = async (_, options) => {
      const key = options.headers['Idempotency-Key'];
      if (!ventas.has(key)) {
        ventas.set(key, { body: options.body, id: ++commits });
        throw new Error('Commit guardado, respuesta perdida');
      }
      assert.equal(options.body, ventas.get(key).body);
      return respuesta({ ok: true, id_venta: ventas.get(key).id });
    };
    const original = pagina({ storage, locks, fetch: backend });
    preparar(original);
    original.el('cliente').value = '2';
    original.el('recibido').value = '100';
    original.ejecutar('actualizarCambio()');
    await original.ejecutar('confirmarCobro()');
    const antes = original.guardado();
    assert.equal(commits, 1);
    original.cerrar();
    const recargada = pagina({ storage, locks, fetch: backend });
    assert.deepEqual(recargada.guardado().intento, antes.intento);
    recargada.ejecutar('abrirCobro()');
    assert.equal(recargada.el('cliente').value, '2');
    assert.equal(Number(recargada.el('recibido').value), 100);
    assert.equal(recargada.el('cambio').textContent, 'Q 90.00');
    await recargada.ejecutar('confirmarCobro()');
    assert.equal(recargada.peticiones[0].headers['Idempotency-Key'], original.peticiones[0].headers['Idempotency-Key']);
    assert.equal(recargada.peticiones[0].body, original.peticiones[0].body);
    assert.equal(commits, 1);
    assert.equal(recargada.destino(), '/ventas/1/ticket');
    assert.equal(recargada.guardado().intento, null);
    assert.deepEqual(recargada.guardado().items, {});
  },

  async producto_ausente() {
    const original = await intentoSinRespuesta();
    const snapshot = original.guardado().intento;
    original.cerrar();
    const recargada = pagina({ storage: original.storage, productos: [] });
    assert.equal(recargada.estado().productos[0].estadoRecuperado, true);
    assert.match(recargada.el('carrito').innerHTML, /Cuaderno/);
    assert.doesNotMatch(recargada.el('catalogo').innerHTML, /Cuaderno/);
    assert.deepEqual(recargada.guardado().intento, snapshot);
    recargada.ejecutar('abrirCobro()');
    await recargada.ejecutar('confirmarCobro()');
    assert.equal(recargada.peticiones[0].body, original.peticiones[0].body);
  },

  async sesion_csrf() {
    for (const devolver of [
      () => ({ ...respuesta({}, 200, 'text/html'), redirected: true }),
      () => respuesta({}, 401),
      () => respuesta({}, 400, 'text/html'),
      () => respuesta({ ok: false, error: 'CSRF vencido' }, 400)
    ]) {
      const page = pagina({ fetch: async () => devolver() });
      preparar(page);
      await page.ejecutar('confirmarCobro()');
      const snapshot = page.guardado().intento;
      assert.ok(snapshot);
      assert.equal(page.el('cliente').disabled, true);
      page.ejecutar('vaciar(); borrar(1); cambiar(1, "cantidad", 7); agregar(1);');
      assert.deepEqual(page.guardado().intento, snapshot);
      assert.equal(page.estado().items['1'].cantidad, 1);
      page.cerrar();
      const recargada = pagina({ storage: page.storage });
      assert.deepEqual(recargada.guardado().intento, snapshot);
    }
  },

  async rechazo_definitivo() {
    let primera = true;
    const page = pagina({ fetch: async () => {
      if (primera) {
        primera = false;
        return respuesta({ ok: false, codigo: 'cobro_rechazado', error: 'Recibido insuficiente' }, 400);
      }
      return respuesta({ ok: true, id_venta: 92 });
    } });
    preparar(page);
    page.el('recibido').value = '1';
    await page.ejecutar('confirmarCobro()');
    assert.equal(page.guardado().intento, null);
    assert.equal(page.el('recibido').disabled, false);
    assert.ok(page.guardado().clave);
    page.el('recibido').value = '20';
    page.ejecutar('cambiar(1, "cantidad", 2)');
    await page.ejecutar('confirmarCobro()');
    assert.equal(page.peticiones[0].headers['Idempotency-Key'], page.peticiones[1].headers['Idempotency-Key']);
    assert.equal(JSON.parse(page.peticiones[1].body).items[0].cantidad, 2);
    assert.equal(JSON.parse(page.peticiones[1].body).recibido, '20');
    assert.equal(page.destino(), '/ventas/92/ticket');
  },

  async pestana_duplicada() {
    const locks = locksCompartidos();
    const original = await intentoSinRespuesta({ locks });
    const antes = original.guardado();
    const duplicada = pagina({ storage: new Map(original.storage), locks });
    assert.equal(duplicada.estado().posListo, true);
    assert.equal(duplicada.guardado().intento, null);
    assert.deepEqual(duplicada.guardado().items, {});
    assert.notEqual(duplicada.guardado().tab, antes.tab);
    assert.deepEqual(original.guardado(), antes);
    preparar(duplicada);
    await duplicada.ejecutar('confirmarCobro()');
    assert.notEqual(duplicada.peticiones[0].headers['Idempotency-Key'], antes.intento.clave);
  },

  async turno_cerrado() {
    const nueva = pagina({ abierto: false });
    assert.equal(nueva.el('abrir-cobro').disabled, true);
    nueva.ejecutar('agregar(1); abrirCobro()');
    assert.match(nueva.el('aviso-pos').textContent, /Abre caja/);
    const original = await intentoSinRespuesta();
    original.cerrar();
    const recuperada = pagina({ abierto: false, storage: original.storage });
    assert.equal(recuperada.el('abrir-cobro').disabled, false);
    recuperada.ejecutar('abrirCobro()');
    assert.equal(recuperada.el('confirmar').textContent, 'REINTENTAR MISMO COBRO');
    await recuperada.ejecutar('confirmarCobro()');
    assert.equal(recuperada.destino(), '/ventas/91/ticket');
    assert.equal(recuperada.el('abrir-cobro').disabled, true);
  }
};

(async () => {
  const caso = process.argv[2];
  assert.ok(casos[caso], `Caso inexistente: ${caso}`);
  await casos[caso]();
  process.stdout.write(`OK ${caso}\n`);
})().catch(error => {
  process.stderr.write(error.stack + '\n');
  process.exitCode = 1;
});
