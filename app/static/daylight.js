/* Display-only daylight adapter.
 *
 * The server owns the sunrise/sunset window and sends it in view.daylight.
 * This file never estimates sun times: it parses the server values and turns
 * them into the solar-card icon, the dawn/dusk sky opacity and the battery
 * caption's fallback sunset clock.
 *
 * Classic IIFE, no build step, no timers.  The defaults are only a first-frame
 * and old-server fallback; the server window replaces them on the first view.
 */
(function () {
  "use strict";

  var DEFAULT_SUNRISE_MINUTES = 7 * 60;
  var DEFAULT_SUNSET_MINUTES = 18 * 60;
  var SKY_TRANSITION_MINUTES = 60;
  var DAY_MINUTES = 24 * 60;

  var sunrise = DEFAULT_SUNRISE_MINUTES;
  var sunset = DEFAULT_SUNSET_MINUTES;

  function smoothstep(t) {
    t = Math.max(0, Math.min(1, t));
    return t * t * (3 - 2 * t);
  }

  function parseClock(text) {
    var m = /^(\d{1,2}):(\d{2})$/.exec(text || "");
    if (!m) return null;
    var hour = Number(m[1]);
    var minute = Number(m[2]);
    if (!isFinite(hour) || !isFinite(minute)) return null;
    if (hour > 24 || minute > 59) return null;
    if (hour === 24 && minute !== 0) return null;
    return hour * 60 + minute;
  }

  function formatClock(minutes) {
    var value = Number(minutes);
    if (!isFinite(value)) return "";
    if (value === DAY_MINUTES) return "24:00";
    value = ((value % DAY_MINUTES) + DAY_MINUTES) % DAY_MINUTES;
    var hour = Math.floor(value / 60);
    var minute = Math.round(value % 60);
    if (minute === 60) {
      hour = (hour + 1) % 24;
      minute = 0;
    }
    return (hour < 10 ? "0" : "") + hour + ":" +
           (minute < 10 ? "0" : "") + minute;
  }

  function normalise(minutes) {
    var value = Number(minutes);
    if (!isFinite(value)) return null;
    return ((value % DAY_MINUTES) + DAY_MINUTES) % DAY_MINUTES;
  }

  /* Half-open interval on the 24-hour circle, so a dawn before midnight and a
     dusk after it both work. */
  function inSpan(minutes, start, end) {
    var m = normalise(minutes);
    var a = normalise(start);
    var b = normalise(end);
    if (m === null || a === null || b === null) return false;
    var span = b - a;
    if (span <= 0) span += DAY_MINUTES;
    var offset = m - a;
    if (offset < 0) offset += DAY_MINUTES;
    return offset < span;
  }

  function sunTimes() {
    return {
      sunrise: sunrise,
      sunset: sunset,
      transition: SKY_TRANSITION_MINUTES
    };
  }

  function configure(config) {
    if (!config || typeof config !== "object") return false;
    var nextSunrise = parseClock(config.sunrise);
    var nextSunset = parseClock(config.sunset);
    if (nextSunrise === null || nextSunset === null) return false;
    if (!(nextSunrise >= 0 && nextSunrise < nextSunset &&
          nextSunset <= DAY_MINUTES)) {
      return false;
    }
    var changed = nextSunrise !== sunrise || nextSunset !== sunset;
    sunrise = nextSunrise;
    sunset = nextSunset;
    return changed;
  }

  function clear() {
    sunrise = DEFAULT_SUNRISE_MINUTES;
    sunset = DEFAULT_SUNSET_MINUTES;
  }

  function clockMinutes(clock) {
    return parseClock(clock);
  }

  function minutesNow(now, tz) {
    var date = now instanceof Date
      ? now : new Date(now === undefined ? Date.now() : now);
    if (isNaN(date.getTime())) return null;
    var opts = { hour: "2-digit", minute: "2-digit", hourCycle: "h23" };
    if (tz) opts.timeZone = tz;
    try {
      var text = new Intl.DateTimeFormat("en-GB", opts).format(date);
      var m = /^(\d{1,2}):(\d{2})$/.exec(text);
      if (m) return (Number(m[1]) % 24) * 60 + Number(m[2]);
    } catch (e) {
      /* Fall through to the host clock. */
    }
    return date.getHours() * 60 + date.getMinutes();
  }

  function sunWindow(sun) {
    if (sun && isFinite(Number(sun.sunrise)) &&
        isFinite(Number(sun.sunset))) {
      return sun;
    }
    return sunTimes();
  }

  function nightAmount(minutes, sun) {
    var m = normalise(minutes);
    if (m === null) return null;
    var window = sunWindow(sun);
    var rise = Number(window.sunrise);
    var set = Number(window.sunset);
    var transition = Number(window.transition);
    if (!isFinite(transition) || transition <= 0) {
      transition = SKY_TRANSITION_MINUTES;
    }
    /* A full-day window has no dawn or dusk edges to cross. */
    if (set - rise >= DAY_MINUTES) return 0;
    var dawnStart = rise - transition;
    var duskEnd = set + transition;
    if (inSpan(m, dawnStart, rise)) {
      var dawnOffset = normalise(m - normalise(dawnStart));
      return 1 - smoothstep(dawnOffset / transition);
    }
    if (inSpan(m, set, duskEnd)) {
      var duskOffset = normalise(m - normalise(set));
      return smoothstep(duskOffset / transition);
    }
    return inSpan(m, duskEnd, dawnStart) ? 1 : 0;
  }

  /* Day includes both sunrise and sunset minutes; the dusk crossfade starts
     at sunset with zero night, so the boundary reads as day. */
  function isDaylight(minutes, sun) {
    var m = normalise(minutes);
    if (m === null) return false;
    var window = sunWindow(sun);
    var rise = normalise(window.sunrise);
    var set = normalise(window.sunset);
    if (rise === null || set === null) return false;
    if (Number(window.sunset) - Number(window.sunrise) >= DAY_MINUTES) return true;
    return m === set || inSpan(m, rise, set);
  }

  function solarIcon(generating, minutes, sun) {
    if (generating) return "sun";
    return isDaylight(minutes, sun) ? "cloud" : "moon";
  }

  function skyState(night) {
    if (night >= 1) return "night";
    if (night <= 0) return "day";
    return "dusk";
  }

  function sunsetClock(sun) {
    return formatClock(sunWindow(sun).sunset);
  }

  window.Daylight = {
    DEFAULT_SUNRISE_MINUTES: DEFAULT_SUNRISE_MINUTES,
    DEFAULT_SUNSET_MINUTES: DEFAULT_SUNSET_MINUTES,
    SKY_TRANSITION_MINUTES: SKY_TRANSITION_MINUTES,
    configure: configure,
    clear: clear,
    sunTimes: sunTimes,
    clockMinutes: clockMinutes,
    minutesNow: minutesNow,
    nightAmount: nightAmount,
    isDaylight: isDaylight,
    solarIcon: solarIcon,
    skyState: skyState,
    sunsetClock: sunsetClock
  };
})();
