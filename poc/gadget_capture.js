// Frida gadget plaintext scanner for the Anker app.
// Finds plan drafts and the separate AC-power-limit call in readable memory.

const TARGETS = [
  ['plan',  '70 65 61 6b 5f 76 61 6c 6c 65 79 5f 70 72 69 63 65 73'], // peak_valley_prices
  ['limit', '61 63 5f 70 6f 77 65 72 5f 6c 69 6d 69 74'],             // ac_power_limit
  ['attrs', '73 65 74 5f 64 65 76 69 63 65 5f 61 74 74 72 73'],       // set_device_attrs
  ['cmd',   '64 65 76 69 63 65 5f 63 6f 6d 6d 61 6e 64'],             // device_command
  ['http',  '5a 58 48 74 74 70 53 65 72 76 69 63 65'],                 // ZXHttpService
];

let busy = false;

function scan() {
  if (busy) return;
  busy = true;
  try {
    Process.enumerateRanges('rw-').forEach(function (range) {
      if (range.size > 400 * 1024 * 1024) return;
      TARGETS.forEach(function (t) {
        try {
          Memory.scanSync(range.base, range.size, t[1]).forEach(function (hit) {
            try {
              // Read a little before the marker so the JSON prefix is included.
              const start = hit.address.sub(8192);
              let data = '';
              try { data = start.readUtf8String(65536); }
              catch (e) { data = hit.address.readUtf8String(65536); }
              if (data) send({ tag: t[0], addr: String(hit.address), data: data });
            } catch (e) {}
          });
        } catch (e) {}
      });
    });
  } catch (e) {
    send({ tag: 'error', data: String(e) });
  }
  busy = false;
}

setInterval(scan, 1000);
scan();
send({ tag: 'ready', data: String(Date.now()) });
