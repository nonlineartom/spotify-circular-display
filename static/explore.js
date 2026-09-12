/* The cover browser owns no playback state. The kiosk supplies a small bridge. */
(function (root) {
  "use strict";

  const SECTION_NAMES = {discover:"Discover",all:"All",made_for_you:"Made for you",vinyls:"Vinyls",rotation:"Top listening",deeper:"Deeper cuts",yours:"Playlists",saved:"Albums",recent:"Recently played",house:"House picks"};
  function buildLibraryItems(data) {
    const unique = new Map();
    for (const section of (data && data.sections) || []) {
      for (const item of section.items || []) {
        if (!item || !/^spotify:(album|playlist):/.test(item.uri || "")) continue;
        if (!unique.has(item.uri)) unique.set(item.uri, {...item, sections:[]});
        const record = unique.get(item.uri);
        if (!record.sections.includes(section.id)) record.sections.push(section.id);
      }
    }
    return [...unique.values()];
  }

  function discoveryRank(item) {
    const sections = item.sections || [];
    return sections.includes("deeper") ? 0 : sections.includes("house") ? 1 : sections.includes("recent") ? 2 : 3;
  }

  function filterLibraryItems(items, options = {}) {
    const section = options.section || "discover";
    const searchable = value => String(value).normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLocaleLowerCase().replace(/[’‘]/g, "'");
    const words = searchable(options.query || "").trim().split(/\s+/).filter(Boolean);
    let results = items.filter(item => {
      const text = searchable(`${item.title || ""} ${item.subtitle || ""}`);
      return (section === "discover" || section === "all" || (item.sections || []).includes(section)) && words.every(word => text.includes(word));
    });
    const sort = options.sort || "recommended";
    if (sort === "title" || sort === "artist") {
      const field = sort === "artist" ? "subtitle" : "title";
      results.sort((a,b) => String(a[field] || "").localeCompare(String(b[field] || "")));
    } else if (section === "discover") results.sort((a,b) => discoveryRank(a) - discoveryRank(b));
    return results;
  }

  function discoveryReason(item) {
    const sections = item.sections || [];
    if (item.quick_kind === "discover_weekly") return "Your weekly discoveries";
    if (item.quick_kind === "release_radar") return "New music from artists you follow";
    if (sections.includes("made_for_you")) return "Made for your Spotify account";
    if (sections.includes("vinyls")) return "From your vinyl collection";
    if (sections.includes("deeper")) return "More from artists you've played";
    if (sections.includes("house")) return "A pick from the house collection";
    if (sections.includes("recent")) return "Worth another spin";
    if (sections.includes("rotation")) return "From your top listening";
    if (sections.includes("saved")) return "From your saved albums";
    return "From your playlists";
  }

  function formatAlbumMetadata(metadata = {}) {
    if (!metadata || typeof metadata !== "object") return "";
    const date = String(metadata.release_date || ""), precision = metadata.release_date_precision;
    let released = "";
    if (/^\d{4}(?:-\d{2}(?:-\d{2})?)?$/.test(date)) {
      const [year,month = 1,day = 1] = date.split("-").map(Number);
      const parsed = new Date(Date.UTC(year, month - 1, day));
      if (year >= 1000 && parsed.getUTCFullYear() === year && parsed.getUTCMonth() === month - 1 && parsed.getUTCDate() === day) {
        const level = precision || (date.length === 4 ? "year" : date.length === 7 ? "month" : "day");
        if (level === "year") released = String(year);
        else if (level === "month" && date.length >= 7) released = new Intl.DateTimeFormat("en-GB",{month:"long",year:"numeric",timeZone:"UTC"}).format(parsed);
        else if (level === "day" && date.length === 10) released = new Intl.DateTimeFormat("en-GB",{day:"numeric",month:"long",year:"numeric",timeZone:"UTC"}).format(parsed);
      }
    }
    const label = typeof metadata.label === "string" ? metadata.label.trim() : "";
    const facts = [];
    if (released) facts.push(`Released ${released}${label ? ` on ${label}` : ""}.`);
    const length = [];
    if (Number.isInteger(metadata.total_tracks) && metadata.total_tracks > 0) length.push(`${metadata.total_tracks} ${metadata.total_tracks === 1 ? "track" : "tracks"}`);
    if (Number.isFinite(metadata.duration_ms) && metadata.duration_ms >= 60000) {
      const minutes = Math.round(metadata.duration_ms / 60000);
      length.push(`${minutes} ${minutes === 1 ? "minute" : "minutes"}`);
    }
    if (length.length) facts.push(length.join(", ") + ".");
    if (label && !released) facts.push(`Label: ${label}.`);
    return facts.join(" ");
  }

  function pageLibraryItems(items, page, pageSize = 6) {
    const size = Math.max(1, Math.min(50, Math.floor(Number(pageSize)) || 6));
    const pageCount = Math.max(1, Math.ceil(items.length / size));
    const current = Math.max(0, Math.min(pageCount - 1, Math.floor(Number(page)) || 0));
    return {items:items.slice(current * size, (current + 1) * size), page:current, pageCount, total:items.length};
  }

  function libraryHoneycombPosition(index) {
    if (!index) return {x:0,y:0};
    const directions = [[-1,1],[-1,0],[0,-1],[1,-1],[1,0],[0,1]];
    let n = 1;
    for (let ring = 1; ring <= 20; ring++) {
      let q = ring, r = 0;
      for (const [dq,dr] of directions) {
        for (let step = 0; step < ring; step++) {
          if (n++ === index) return {x:166 * (q + r / 2), y:166 * Math.sqrt(3) / 2 * r};
          q += dq; r += dr;
        }
      }
    }
    return {x:0,y:0};
  }

  // A full-size centre and a smooth fringe. Integrating the size falloff also
  // compresses positions, so shrinking covers don't leave a sparse fixed grid.
  // This is an approximation, not Apple's unpublished projection. See docs.
  function libraryLens(distance) {
    const inner = .3, range = 1.4;
    const t = Math.max(0, Math.min(1, (distance - inner) / range));
    return {scale:1 - t*t*(3 - 2*t), radius:distance <= inner ? distance : inner + range*(t - t*t*t + .5*t*t*t*t)};
  }

  function projectLibraryCover(point, options = {}) {
    const {panX=0, panY=0, zoom=1, width=760, height=450, reduceMotion=false} = options;
    const x = point.x*zoom + panX, y = point.y*zoom + panY;
    if (reduceMotion) return {x,y,scale:zoom,visible:Math.abs(x)<width/2+72*zoom && Math.abs(y)<height/2+72*zoom};
    const distance = Math.hypot(x/(width/2), y/(height/2));
    const lens = libraryLens(distance), ratio = distance ? lens.radius/distance : 1;
    return {x:x*ratio,y:y*ratio,scale:zoom*lens.scale,visible:lens.scale>.025};
  }

  function unprojectLibraryPoint(point, options = {}) {
    const {width=760,height=450,reduceMotion=false} = options;
    if (reduceMotion) return {...point};
    const radius = Math.hypot(point.x/(width/2), point.y/(height/2));
    if (radius <= .3) return {...point};
    let low=.3, high=1.7;
    for(let i=0;i<24;i++) {const mid=(low+high)/2;if(libraryLens(mid).radius<Math.min(.999,radius))low=mid;else high=mid;}
    const ratio=(low+high)/2/radius;
    return {x:point.x*ratio,y:point.y*ratio};
  }

  function zoomLibraryAt(view, nextZoom, anchor = {x:0,y:0}, options = {}) {
    const zoom=Math.max(.6,Math.min(1.5,nextZoom)), raw=unprojectLibraryPoint(anchor,options), ratio=zoom/view.zoom;
    return {zoom,panX:raw.x-(raw.x-view.panX)*ratio,panY:raw.y-(raw.y-view.panY)*ratio};
  }

  function stepLibraryMotion(motion, bounds, seconds) {
    const dt=Math.max(0,Math.min(.034,seconds));
    function axis(position, velocity, min, max) {
      const edge=Math.max(min,Math.min(max,position));
      if(position!==edge) {
        const displacement=position-edge, spring=18, c=velocity+spring*displacement, decay=Math.exp(-spring*dt);
        return [edge+(displacement+c*dt)*decay,(velocity-spring*c*dt)*decay];
      }
      const next=velocity*Math.exp(-5*dt);
      return [position+(velocity-next)/5,next];
    }
    let [x,vx]=axis(motion.x,motion.vx,bounds.minX,bounds.maxX);
    let [y,vy]=axis(motion.y,motion.vy,bounds.minY,bounds.maxY);
    const targetX=Math.max(bounds.minX,Math.min(bounds.maxX,x)),targetY=Math.max(bounds.minY,Math.min(bounds.maxY,y));
    const done=Math.hypot(vx,vy)<4 && Math.hypot(x-targetX,y-targetY)<.5;
    if(done) {x=targetX;y=targetY;vx=0;vy=0;}
    return {x,y,vx,vy,done};
  }

  const helpers = {buildLibraryItems,filterLibraryItems,discoveryReason,formatAlbumMetadata,pageLibraryItems,libraryHoneycombPosition,projectLibraryCover,unprojectLibraryPoint,zoomLibraryAt,stepLibraryMotion};
  if (typeof module !== "undefined" && module.exports) module.exports = helpers;
  if (!root || !root.document) return;

  root.createDisplayExplorer = function createDisplayExplorer(bridge) {
    const $ = id => document.getElementById(id);
    const explore = $("explore-modal"), controls = $("controls-modal");
    const grid = $("explore-grid"), map = $("explore-map"), belt = $("explore-map-belt");
    let mode = null, restoreFocus = null, selected = null, selectedFocusUri = null;
    let items = [], section = "discover", page = 0, layout = "mosaic", loading = false, loadError = "", refreshing = null;
    let requestEpoch = 0, libraryEpoch = 0, controlEpoch = 0;
    let detailAbort = null, refreshAbort = null, refreshTimer = null, controlTimer = null;
    let playPending = false, playController = null, controlPending = false;
    let zoom = 1, panX = 0, panY = 0, pointers = new Map(), drag = null, movedUntil = 0;
    let gridScrollTop = 0;
    let detailMotion = null, selectedMetadata = null, restoringCoverFocus = false;
    let mapCovers = [], mapFrame = null, mapMotion = null, mapFrameTime = 0;
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    let libraryFingerprint = "", libraryProfileEpoch = null;
    let personalStatus = null;
    const searchKeyboard = root.createTouchKeyboard({element:$("explore-keyboard-keys"), onDone:()=>hideSearchKeyboard(true)});
    const collectionEditor = root.createCollectionEditor({
      getProfile:()=>bridge.getProfile(), acceptProfile:(data,revision)=>bridge.acceptProfile(data,revision),
      onProfileChanged:(data,revision)=>{bridge.acceptProfile(data,revision);invalidateProfile();},
      activity:()=>bridge.activity(), onChanged:()=>{
        libraryFingerprint="";libraryEpoch++;
        if(refreshAbort)refreshAbort.abort();refreshAbort=null;refreshing=null;
        if(mode==="explore")refreshLibrary();
      }
    });
    try { if (localStorage.recordBrowserLayout === "grid") layout = "grid"; } catch (_) {}

    function button(text, className, action) {
      const el = document.createElement("button"); el.type = "button"; el.className = className; el.textContent = text;
      el.addEventListener("click", action); return el;
    }

    function artwork(item, lazy = false) {
      const fallback = document.createElement("span"); fallback.className = "cover-fallback"; fallback.textContent = item.title || "Untitled record";
      if (!/^(https?:\/\/|\/static\/)/.test(item.image || "")) return fallback;
      const img = document.createElement("img"); img.className = "cover-art"; img.src = item.image; img.alt = ""; img.draggable = false; img.decoding = "async";
      if (lazy) img.loading = "lazy";
      img.addEventListener("error", () => img.replaceWith(fallback), {once:true}); return img;
    }

    function status(message) { $("explore-status").textContent = message || ""; }
    function isOpen() { return mode !== null; }

    function invalidateProfile(retryDelay = 0) {
      collectionEditor.close(false);
      personalStatus=null;
      // Receiver identity is a boundary for every private surface, including
      // hidden cover nodes and outstanding detail/play requests.
      libraryEpoch++;
      if(refreshAbort) refreshAbort.abort();
      refreshAbort=null;refreshing=null;loading=false;
      clearTimeout(refreshTimer);refreshTimer=null;
      stopMapMotion();pointers.clear();drag=null;
      clearSelection();selectedFocusUri=null;
      items=[];libraryFingerprint="";libraryProfileEpoch=null;
      section="discover";page=0;gridScrollTop=0;loadError="";
      $("explore-search").value="";
      belt.replaceChildren();grid.replaceChildren();mapCovers=[];
      $("search-matches").replaceChildren();
      if(mode==="explore") {
        render();
        refreshTimer=setTimeout(()=>{refreshTimer=null;refreshLibrary();},retryDelay);
      }
    }

    function acceptLibraryProfile(data, revision) {
      if(!bridge.getProfile) return true;
      if(data && typeof data.profile_epoch==="string" && bridge.acceptProfile && bridge.acceptProfile(data,revision)) return true;
      invalidateProfile(3000);
      return false;
    }

    function show(which) {
      if (mode === which) return;
      if (mode) close(false);
      restoreFocus = document.activeElement;
      bridge.onOpen(); mode = which;
      explore.hidden = which !== "explore"; controls.hidden = which !== "controls";
      bridge.setModal(true, which === "explore" ? explore : controls);
      if (which === "explore") {
        $("explore-close").focus({preventScroll:true});
        render(); refreshLibrary();
      } else {
        $("controls-close").focus({preventScroll:true}); updateControls();
        controlTimer = setInterval(updateControls, 1000);
        checkBacklight();
      }
    }

    function close(restore = true) {
      if (!mode) return;
      const old = mode; mode = null;
      collectionEditor.close(false);
      stopMapMotion();
      hideSearchKeyboard(false);
      clearTimeout(refreshTimer); refreshTimer=null; clearInterval(controlTimer);
      libraryEpoch++; controlEpoch++;
      if (refreshAbort) refreshAbort.abort();
      refreshAbort=null; refreshing=null; loading=false; controlPending=false;
      clearSelection();
      pointers.clear(); drag = null;
      explore.hidden = true; controls.hidden = true;
      bridge.setModal(false, old === "explore" ? explore : controls);
      if (restore && restoreFocus && restoreFocus.isConnected && !restoreFocus.closest("[hidden]")) restoreFocus.focus({preventScroll:true});
      else if (restore) $("open-records").focus({preventScroll:true});
      restoreFocus = null;
    }

    function updateTabs() {
      const ids = ["discover","made_for_you","vinyls","all",...Object.keys(SECTION_NAMES).filter(id => !["discover","all","made_for_you","vinyls"].includes(id) && items.some(item => item.sections.includes(id)))];
      if (!ids.includes(section)) section = "discover";
      const focused = document.activeElement && document.activeElement.dataset.section;
      $("explore-tabs").replaceChildren(...ids.map(id => {
        const el = button(SECTION_NAMES[id], "", () => {section=id;if(id==="made_for_you")layout="grid";page=0;gridScrollTop=0;resetMap();render();$("explore-tabs").querySelector(`[data-section="${id}"]`).focus({preventScroll:true});});
        el.dataset.section = id; el.setAttribute("role","tab"); el.setAttribute("aria-selected",String(section === id)); el.tabIndex = section === id ? 0 : -1;
        return el;
      }));
      if (focused && ids.includes(focused)) $("explore-tabs").querySelector(`[data-section="${focused}"]`).focus({preventScroll:true});
    }

    function results() {
      const matches=filterLibraryItems(items,{section,query:$("explore-search").value});
      return section==="made_for_you"?matches.filter(item=>item.quick_kind==="mix"):matches;
    }

    function renderQuickLinks() {
      $("explore-personal-links").hidden=section!=="made_for_you";
      if(section!=="made_for_you")return;
      $("personal-quick-buttons").replaceChildren(...[["discover_weekly","Discover Weekly"],["release_radar","Release Radar"]].map(([kind,label])=>{
        const item=items.find(record=>record.quick_kind===kind),el=button(label,"utility-button",()=>{if(item)showDetail(item);});
        el.disabled=!item;return el;
      }));
      const refresh=button(loading?"Refreshing…":"Refresh","utility-button personal-refresh",()=>refreshLibrary(true));
      refresh.disabled=loading;refresh.setAttribute("aria-label","Refresh Spotify playlists");$("personal-quick-buttons").appendChild(refresh);
      const mixes=items.filter(item=>item.quick_kind==="mix").length;
      $("personal-quick-status").textContent=personalStatus?.status==="ready"?`${mixes} Spotify ${mixes===1?"mix":"mixes"} to explore below`:personalStatus?.message||"Finding the live account’s weekly playlists and mixes…";
    }

    function openSearchKeyboard() {
      if (mode !== "explore" || selected) return;
      stopMapMotion(); pointers.clear(); drag=null;
      $("explore-search-keyboard").hidden = false;
      $("explore-results").hidden = true;
      $("explore-search").setAttribute("aria-expanded", "true");
      searchKeyboard.open($("explore-search"), {doneLabel:"Show records"});
      renderSearchMatches(results());
    }

    function hideSearchKeyboard(focusSearch = false) {
      searchKeyboard.close();
      $("explore-search-keyboard").hidden = true;
      $("explore-results").hidden = false;
      $("explore-search").setAttribute("aria-expanded", "false");
      if (mode === "explore" && !selected && layout === "mosaic") positionMap();
      if (focusSearch) $("explore-search").focus({preventScroll:true});
    }

    function renderSearchMatches(filtered) {
      $("search-match-count").textContent = `${filtered.length} ${filtered.length === 1 ? "record" : "records"} in ${SECTION_NAMES[section]}`;
      const matches = filtered.slice(0, 3).map(item => {
        const el = button("", "search-match", event => showDetail(item,event.currentTarget));
        el.dataset.uri = item.uri;
        el.setAttribute("aria-label", `${item.title} — ${item.subtitle || ""}. View details`);
        const art = document.createElement("span"); art.className = "search-match-art"; art.appendChild(artwork(item));
        const copy = document.createElement("span"); copy.className = "search-match-copy";
        const title = document.createElement("strong"), artist = document.createElement("span");
        title.textContent = item.title; artist.textContent = item.subtitle || "View record";
        copy.append(title, artist); el.append(art, copy); return el;
      });
      if (!matches.length) {
        const empty = document.createElement("p"); empty.className = "search-no-matches";
        empty.textContent = loading ? "Loading your collection…" : loadError || (items.length ? "No matches yet. Try a shorter name or tap All." : "Connect a library in Settings to find your records.");
        matches.push(empty);
      }
      $("search-matches").replaceChildren(...matches);
    }

    function render() {
      if (mode !== "explore" || selected) return;
      cancelDetailMotion();
      stopMapMotion(); pointers.clear(); drag=null;
      updateTabs();
      renderQuickLinks();
      const filtered = results(), batch = pageLibraryItems(filtered,page,37); page=batch.page;
      const visibleItems = layout === "mosaic" ? batch.items : filtered;
      if (searchKeyboard.isOpen()) renderSearchMatches(filtered);
      $("explore-description").textContent = section === "vinyls" ? "Your records, ready to play on Spotify. Tap a sleeve to play or queue the album." : section === "discover" ? "More from the artists you play, and favourites worth revisiting." : "Take your time. Tap a cover to see more before you play.";
      map.hidden = layout !== "mosaic" || !filtered.length;
      $("explore-covers").classList.toggle("mosaic",layout === "mosaic");
      grid.hidden = layout !== "grid" || !filtered.length;
      $("explore-map-tools").hidden = layout !== "mosaic" || !filtered.length;
      $("explore-mosaic").setAttribute("aria-pressed",String(layout === "mosaic"));
      $("explore-grid-view").setAttribute("aria-pressed",String(layout === "grid"));
      belt.replaceChildren(); grid.replaceChildren(); mapCovers=[];
      visibleItems.forEach((item,index) => {
        const el = button("",layout === "mosaic" ? "map-cover" : "grid-cover",event => {if(performance.now() >= movedUntil) showDetail(item,event.currentTarget);});
        el.dataset.uri = item.uri; el.setAttribute("aria-label",`${item.title} — ${item.subtitle || ""}. ${discoveryReason(item)}. View details`);
        if (layout === "mosaic") {
          const p = libraryHoneycombPosition(index); el.appendChild(artwork(item));
          mapCovers.push({el,point:p,item});
          el.addEventListener("focus",() => {if(restoringCoverFocus)return;stopMapMotion();panX=-p.x*zoom;panY=-p.y*zoom;positionMap();$("explore-hint").textContent=`${item.title} · ${item.subtitle || discoveryReason(item)}`;});
          el.addEventListener("pointerenter",() => {$("explore-hint").textContent=`${item.title} · ${item.subtitle || discoveryReason(item)}`;});
          belt.appendChild(el);
        } else {
          const pic = document.createElement("span"); pic.className="cover-picture";pic.appendChild(artwork(item,true));el.appendChild(pic);
          const title=document.createElement("span");title.className="cover-title";title.textContent=item.title;
          const artist=document.createElement("span");artist.className="cover-artist";artist.textContent=item.subtitle || discoveryReason(item);
          el.append(title,artist);grid.appendChild(el);
        }
      });
      if (layout === "grid") grid.scrollTop = gridScrollTop;
      clampMap(); positionMap();
      $("explore-empty").hidden = !!filtered.length;
      $("explore-surprise").disabled = !filtered.some(item=>item.playable!==false);
      $("explore-prev").hidden = $("explore-next").hidden = layout === "grid";
      $("explore-prev").disabled = layout === "grid" || page <= 0; $("explore-next").disabled = layout === "grid" || page >= batch.pageCount-1;
      const total = `${filtered.length} ${section==="made_for_you"?(filtered.length===1?"mix":"mixes"):(filtered.length===1?"record":"records")}`;
      $("explore-count").textContent = filtered.length ? total + (layout === "mosaic" ? ` · ${page+1} of ${batch.pageCount}` : "") : "";
      $("explore-hint").textContent = layout === "mosaic" ? "Drag a cover towards the centre · tap to take a look" : "Swipe up to explore · tap a cover for details";
      $("explore-retry").hidden = !loadError && !loading;
      $("explore-retry").disabled = loading;
      $("explore-connect").hidden = !!items.length || loading;
      $("explore-empty-title").textContent = loading ? "Finding your next record…" : loadError ? "The collection is taking a moment." : items.length ? "No matching records." : "Your next record starts here.";
      $("explore-empty-copy").textContent = loadError || (items.length ? "Try another artist, title or collection." : "Connect your Spotify library in Settings. Albums from artists you play will appear here as you listen.");
      if(section==="made_for_you"&&!filtered.length&&!loading&&!loadError) {
        $("explore-empty-title").textContent=personalStatus?.status==="link_required"?"Connect the account you’re listening with.":"Bring your Spotify mixes along.";
        $("explore-empty-copy").textContent=personalStatus?.message||"Save your weekly playlists and mixes in Spotify, then refresh.";
        $("explore-retry").hidden=false;$("explore-retry").disabled=false;$("explore-retry").textContent="Refresh playlists";
        $("explore-connect").hidden=personalStatus?.status!=="link_required";
      } else $("explore-retry").textContent="Try again";
      status(loadError && items.length ? "Showing the last collection. Refresh is unavailable; try again shortly." : "");
    }

    async function refreshLibrary(force = false) {
      force=force===true;
      if (refreshing) return refreshing;
      clearTimeout(refreshTimer); refreshTimer=null;
      const epoch=++libraryEpoch, controller = new AbortController(); refreshAbort = controller;
      const profileRevision=bridge.getProfile ? bridge.getProfile().revision : null;
      const timeout=setTimeout(()=>controller.abort(),15000);
      let needsRender=!items.length || !!loadError || force;
      loading=true; loadError=""; if(!items.length) render();
      if(force)renderQuickLinks();
      refreshing=(async()=>{
        try {
          if(force && bridge.getProfile) {
            const refreshResponse=await fetch("/api/crate/refresh",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({profile_epoch:libraryProfileEpoch||bridge.getProfile().epoch}),signal:controller.signal});
            const refreshed=await refreshResponse.json();
            if(controller.signal.aborted || epoch!==libraryEpoch || mode!=="explore")return;
            if(refreshResponse.status===409 && refreshed.code==="profile_changed") {bridge.acceptProfile(refreshed,profileRevision);invalidateProfile();return;}
            if(!refreshResponse.ok)throw new Error(refreshed.error||"Spotify playlists could not be refreshed. Please try again.");
            if(!acceptLibraryProfile(refreshed,profileRevision) || epoch!==libraryEpoch)return;
          }
          const response=await fetch("/api/crate",{cache:"no-store",signal:controller.signal});
          if(epoch!==libraryEpoch || mode!=="explore") return;
          if(response.status===401 || response.status===403) {
            items=[]; libraryFingerprint=""; if(selected) backToBrowser(true);
            throw new Error("Open Settings to sign in as the owner, then browse the shared collection.");
          }
          if(!response.ok) throw new Error("The collection couldn't be refreshed. Please try again.");
          const data=await response.json();
          if(controller.signal.aborted || epoch!==libraryEpoch || mode!=="explore") return;
          if(!acceptLibraryProfile(data,profileRevision) || epoch!==libraryEpoch) return;
          if(bridge.getProfile) libraryProfileEpoch=data.profile_epoch;
          if(!Array.isArray(data.sections)) throw new Error("The collection couldn't be read. Please try again.");
          if(data.building) {refreshTimer=setTimeout(()=>{refreshTimer=null;refreshLibrary();},3000);return;}
          if(JSON.stringify(personalStatus)!==JSON.stringify(data.made_for_you||null))needsRender=true;
          personalStatus=data.made_for_you||null;
          const next=buildLibraryItems(data), fingerprint=JSON.stringify(next);
          if(fingerprint!==libraryFingerprint) {
            needsRender=true;
            items=next;libraryFingerprint=fingerprint;
            if(selected && !items.some(item=>item.uri===selected.uri)) backToBrowser(true);
            else if(selected) {selected=items.find(item=>item.uri===selected.uri);renderDetailHero();}
          }
        } catch(error) {
          if(epoch===libraryEpoch && mode==="explore") {needsRender=true;loadError=error.name==="AbortError" ? "The collection took too long to respond. Please try again." : error.message;}
        } finally {
          clearTimeout(timeout);
          if(epoch===libraryEpoch && refreshAbort===controller) {
            refreshing=null;loading=false;refreshAbort=null;
            if(mode==="explore") { if(!selected && needsRender) render(); if(!refreshTimer) refreshTimer=setTimeout(()=>{refreshTimer=null;refreshLibrary();},30000); }
          }
        }
      })();
      return refreshing;
    }

    function captureCover(source) {
      if(!source || !source.isConnected) return null;
      const surface=source.querySelector(".cover-picture,.search-match-art") || source;
      const rect=surface.getBoundingClientRect();
      const art=surface.querySelector(".cover-art,.cover-fallback");
      if(!art || rect.width<=0 || rect.height<=0) return null;
      const radius=getComputedStyle(surface).borderTopLeftRadius;
      return {rect,radius:radius.endsWith("%") ? parseFloat(radius)/100 : parseFloat(radius)/surface.offsetWidth || 0,art:art.cloneNode(true)};
    }

    function cancelDetailMotion() {
      const motion=detailMotion;detailMotion=null;
      if(!motion) return;
      for(const animation of motion.animations) animation.cancel();
      if(motion.hiddenArt) motion.hiddenArt.style.visibility=motion.visibility;
      if(motion.ghost) motion.ghost.remove();
    }

    function settleDetailMotion(motion) {
      // The same cleanup handles completion, interruptions and account handoff.
      // No cloned account artwork survives a modal close or profile invalidation.
      Promise.all(motion.animations.map(animation=>animation.finished)).then(()=>{
        if(detailMotion===motion) cancelDetailMotion();
      }).catch(()=>{if(detailMotion===motion)cancelDetailMotion();});
    }

    function flyCover(source, target, motion, duration=680) {
      if(!source || !target || !target.animate) return;
      const rect=target.getBoundingClientRect(), viewport=explore.getBoundingClientRect();
      if(!rect.width || !rect.height || !viewport.width) return;
      const scale=explore.clientWidth/viewport.width;
      const ghost=document.createElement("div");ghost.className="detail-cover-flight";ghost.setAttribute("aria-hidden","true");
      ghost.style.left=(rect.left-viewport.left)*scale+"px";ghost.style.top=(rect.top-viewport.top)*scale+"px";
      ghost.style.width=rect.width*scale+"px";ghost.style.height=rect.height*scale+"px";
      ghost.appendChild(source.art);explore.appendChild(ghost);
      motion.ghost=ghost;motion.hiddenArt=target;motion.visibility=target.style.visibility;
      const endRadius=getComputedStyle(target).borderTopLeftRadius;
      const endFraction=endRadius.endsWith("%") ? parseFloat(endRadius)/100 : parseFloat(endRadius)/target.offsetWidth || 0;
      target.style.visibility="hidden";
      motion.animations.push(ghost.animate([
        {transform:`translate(${(source.rect.left-rect.left)*scale}px,${(source.rect.top-rect.top)*scale}px) scale(${source.rect.width/rect.width},${source.rect.height/rect.height})`,borderRadius:source.radius*100+"%"},
        {transform:"translate(0,0) scale(1)",borderRadius:endFraction*100+"%"},
      ],{duration,easing:"cubic-bezier(0.32, 0.72, 0, 1)",fill:"both"}));
    }

    function animateDetailIn(source) {
      cancelDetailMotion();
      if(reducedMotion.matches || !$("detail-art").animate) return;
      const motion={animations:[]};detailMotion=motion;
      flyCover(source,$("detail-art"),motion);
      const drawer=$("detail-drawer"), copy=$("explore-detail").querySelector(".detail-copy"), actions=$("explore-detail").querySelector(".detail-actions");
      // The artwork lands while the track sleeve emerges from its right edge.
      motion.animations.push(drawer.animate([
        {transform:"translateX(-190px)",clipPath:"inset(0 100% 0 0)",opacity:0},
        {transform:"translateX(0)",clipPath:"inset(0 0 0 0)",opacity:1},
      ],{duration:620,delay:130,easing:"cubic-bezier(0.32, 0.72, 0, 1)",fill:"both"}));
      motion.animations.push(copy.animate([{opacity:0,transform:"translateY(18px)"},{opacity:1,transform:"translateY(0)"}],{duration:480,delay:180,easing:"cubic-bezier(0.16, 1, 0.3, 1)",fill:"both"}));
      motion.animations.push(actions.animate([{opacity:0},{opacity:1}],{duration:320,delay:280,fill:"both"}));
      settleDetailMotion(motion);
    }

    function animateDetailOut(source, cover) {
      if(reducedMotion.matches || !source || !cover || !cover.animate) return;
      const target=cover.querySelector(".cover-picture") || cover;
      const motion={animations:[]};detailMotion=motion;
      flyCover(source,target,motion,540);
      motion.animations.push($("explore-browser").animate([{opacity:0},{opacity:1}],{duration:320,easing:"ease-out",fill:"both"}));
      settleDetailMotion(motion);
    }

    function renderDetailHero() {
      if(!selected) return;
      $("detail-art").replaceChildren(artwork(selected));
      $("detail-title").textContent=selected.title;
      $("detail-subtitle").textContent=selected.subtitle || "";
      $("detail-edition").textContent=selected.edition_note || "";
      $("detail-metadata").textContent=formatAlbumMetadata(selectedMetadata || selected.album_metadata);
      $("detail-reason").textContent=discoveryReason(selected);
      $("detail-play").textContent=playPending ? "Starting…" : selected.uri.startsWith("spotify:playlist:") ? "▶ Play playlist" : "▶ Play album";
      $("detail-queue").hidden=!selected.uri.startsWith("spotify:album:");
      $("detail-note").textContent=selected.uri.startsWith("spotify:playlist:")?"Play starts this playlist on the display.":"Play starts the record now. Queue adds it after your queued tracks.";
      $("detail-play").disabled=playPending || selected.playable===false;
      $("detail-queue").disabled=playPending || selected.playable===false;
      $("detail-add-vinyl").hidden=!selected.uri.startsWith("spotify:album:");
      $("detail-add-vinyl").disabled=playPending||(selected.sections||[]).includes("vinyls");
      $("detail-add-vinyl").textContent=(selected.sections||[]).includes("vinyls")?"In Vinyls ✓":"+ Vinyls";
    }

    function showDetail(item, sourceElement) {
      const source=captureCover(sourceElement || [...document.querySelectorAll(".map-cover,.grid-cover,.search-match")].find(el=>el.dataset.uri===item.uri));
      cancelDetailMotion();
      stopMapMotion(); pointers.clear(); drag=null;
      if (layout === "grid" && !$("explore-results").hidden) gridScrollTop = grid.scrollTop;
      hideSearchKeyboard(false);
      selected=item;selectedFocusUri=item.uri;selectedMetadata=null;
      $("explore-browser").hidden=true;$("explore-detail").hidden=false;
      renderDetailHero();$("detail-back").focus({preventScroll:true});loadDetail();animateDetailIn(source);
    }

    function clearSelection() {
      cancelDetailMotion();
      requestEpoch++;
      if(detailAbort) detailAbort.abort();detailAbort=null;
      if(playController) playController.abort();playController=null;
      playPending=false;selected=null;selectedMetadata=null;
      setPlayDisabled(false);
      $("detail-tracks").replaceChildren();$("detail-art").replaceChildren();
      for(const id of ["detail-title","detail-subtitle","detail-edition","detail-metadata","detail-reason","detail-status","detail-track-count"]) $(id).textContent="";
      $("detail-queue").textContent="+ Queue album";
      $("explore-browser").hidden=false;$("explore-detail").hidden=true;
    }

    function backToBrowser(force = false) {
      if(playPending && force!==true) return;
      const source=force===true ? null : captureCover(detailMotion && detailMotion.ghost || $("detail-art"));
      if(force===true) selectedFocusUri=null;
      clearSelection();render();
      const cover=[...document.querySelectorAll(".map-cover,.grid-cover")].find(el=>el.dataset.uri===selectedFocusUri);
      restoringCoverFocus=true;
      try {(cover || $("explore-close")).focus({preventScroll:true});} finally {restoringCoverFocus=false;}
      if(force!==true) animateDetailOut(source,cover);
    }

    async function loadDetail() {
      const item=selected, epoch=++requestEpoch; if(!item) return;
      if(item.playable===false) {
        $("detail-tracks").replaceChildren();$("detail-retry").hidden=true;
        $("detail-track-count").textContent="";$("detail-track-label").textContent="On this record";
        $("detail-status").textContent=item.availability_note || "This album is unavailable on Spotify.";
        return;
      }
      const profileRevision=bridge.getProfile ? bridge.getProfile().revision : null;
      const profileQuery=bridge.getProfile ? "&profile_epoch="+encodeURIComponent(libraryProfileEpoch || "") : "";
      if(detailAbort) detailAbort.abort();
      const controller=new AbortController();detailAbort=controller;
      const timeout=setTimeout(()=>controller.abort(),15000);
      $("detail-tracks").replaceChildren();$("detail-status").textContent="Loading tracks…";$("detail-retry").hidden=true;$("detail-track-count").textContent="";
      try {
        const response=await fetch("/api/library/item?uri="+encodeURIComponent(item.uri)+profileQuery,{cache:"no-store",signal:controller.signal});
        const data=await response.json();
        if(epoch!==requestEpoch || mode!=="explore" || !selected || selected.uri!==item.uri) return;
        if(response.status===409 && data.code==="profile_changed") {
          if(bridge.acceptProfile) bridge.acceptProfile(data,profileRevision);
          invalidateProfile();return;
        }
        if(response.ok && (!acceptLibraryProfile(data,profileRevision) || epoch!==requestEpoch)) return;
        if(!response.ok) throw new Error(response.status===401 ? "Owner sign-in is needed. Open Settings to reconnect." : data.error || "Tracks are unavailable at the moment.");
        selectedMetadata=data.album_metadata || null;
        $("detail-metadata").textContent=formatAlbumMetadata(selectedMetadata);
        const tracks=Array.isArray(data.tracks) ? data.tracks : [];
        $("detail-track-label").textContent=data.kind==="playlist" ? "About this playlist" : "On this record";
        $("detail-track-count").textContent=tracks.length ? `${tracks.length} tracks` : "";
        if(!playPending) $("detail-status").textContent=data.message || (!tracks.length ? "You can play the full collection using the button above." : "Choose a track to start here.");
        $("detail-retry").hidden=data.tracks_status!=="unavailable";
        $("detail-tracks").replaceChildren(...tracks.slice(0,500).map(track=>{
          const el=button("","detail-track",()=>playSelection(track.uri));
          el.disabled=playPending;
          el.setAttribute("aria-label",`Play ${track.name}`);
          const number=document.createElement("span"),title=document.createElement("span"),time=document.createElement("span");
          number.textContent=track.number || "♪";title.textContent=track.name;time.textContent=formatTime(track.duration_ms);
          el.append(number,title,time);return el;
        }));
      } catch(error) {
        if(epoch!==requestEpoch || !selected || mode!=="explore") return;
        $("detail-status").textContent=error.name==="AbortError" ? "Tracks took too long to load. You can retry or play the full record." : error.message;
        $("detail-retry").hidden=false;
      } finally {clearTimeout(timeout);if(detailAbort===controller)detailAbort=null;}
    }

    async function playSelection(trackUri, queue = false) {
      if(playPending || !selected || selected.playable===false) return;
      const item=selected;playPending=true;
      const epoch=requestEpoch,controller=new AbortController();playController=controller;
      const profileRevision=bridge.getProfile ? bridge.getProfile().revision : null;
      const timeout=setTimeout(()=>controller.abort(),queue ? 45000 : 10000);
      setPlayDisabled(true);renderDetailHero();$("detail-status").textContent=queue ? "Adding the album to your Spotify queue…" : trackUri ? "Starting your selected track…" : "Putting your record on…";
      if(queue) {$("detail-play").textContent="▶ Play album";$("detail-queue").textContent="Queueing…";}
      try {
        const response=await fetch(queue ? "/api/library/queue" : "/api/library/play",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({uri:item.uri,...(trackUri?{track_uri:trackUri}:{}),...(bridge.getProfile?{profile_epoch:libraryProfileEpoch}:{})}),signal:controller.signal});
        const data=await response.json();
        if(epoch!==requestEpoch || mode!=="explore") return;
        if(response.status===409 && data.code==="profile_changed") {
          if(bridge.acceptProfile) bridge.acceptProfile(data,profileRevision);
          invalidateProfile();return;
        }
        if(response.ok && (!acceptLibraryProfile(data,profileRevision) || epoch!==requestEpoch)) return;
        if(!response.ok) throw new Error(response.status===401 ? "Open Settings to reconnect your owner session." : data.error || "Playback is unavailable. Try choosing Pi Display in Spotify first.");
        if(queue) {
          $("detail-status").textContent=data.status==="partial" ? `Added ${data.queued} of ${data.of} tracks. Check the queue in Spotify before adding this album again.` : `Album queued · ${data.queued} tracks. Your music keeps playing.`;
        } else {
          clearTimeout(timeout);
          // Keep the selected sleeve on screen until the player has received
          // its artwork. The bridge flies it straight onto the playback disc.
          const source=captureCover(detailMotion && detailMotion.ghost || $("detail-art"));
          cancelDetailMotion();
          await bridge.onPlayed({item,trackUri,source,signal:controller.signal});
          if(epoch===requestEpoch && mode==="explore" && !controller.signal.aborted) {
            playPending=false;close(false);
          }
        }
      } catch(error) {
        if(epoch===requestEpoch && mode==="explore") $("detail-status").textContent=error.name==="AbortError" ? queue ? "No confirmation yet. Check the queue in Spotify before adding this album again." : "No confirmation from the player yet. Check playback before trying again." : error.message;
      } finally {
        clearTimeout(timeout);
        if(playController===controller) {playController=null;playPending=false;setPlayDisabled(false);renderDetailHero();$("detail-queue").textContent="+ Queue album";}
      }
    }

    function setPlayDisabled(disabled) { $("detail-play").disabled=disabled || !!(selected && selected.playable===false);$("detail-queue").disabled=$("detail-play").disabled;$("detail-back").disabled=disabled;$("detail-tracks").querySelectorAll("button").forEach(el=>el.disabled=disabled); }
    function formatTime(ms) {const sec=Math.max(0,Math.floor((Number(ms)||0)/1000));return `${Math.floor(sec/60)}:${String(sec%60).padStart(2,"0")}`;}
    function updateControls() {
      if(mode!=="controls") return;
      const state=bridge.getState();
      $("controls-now-playing").textContent=state.trackId ? `${state.trackName} · ${state.artistName}` : "Choose something from your record collection.";
      $("controls-play").textContent=state.isPlaying ? "Ⅱ Pause" : "▶ Play";
      ["controls-play","controls-next","controls-previous","controls-tracks","controls-seek"].forEach(id=>$(id).disabled=!state.trackId || controlPending);
      for(const [id,value] of [["volume",state.volume],["brightness",state.brightness]]) {
        if(document.activeElement!==$("controls-"+id)) $("controls-"+id).value=Math.round(value);
        $("controls-"+id+"-value").textContent=Math.round($("controls-"+id).value)+"%";
      }
      if(document.activeElement!==$("controls-seek")) {$("controls-seek").value=Math.round((state.position || 0)/Math.max(1,state.durationMs)*1000);$("controls-position").textContent=formatTime(state.position);}
    }

    async function checkBacklight() {
      const epoch=controlEpoch;
      try { const response=await fetch("/api/backlight");const data=await response.json();if(epoch!==controlEpoch || mode!=="controls")return;
        const local=["127.0.0.1","localhost","[::1]"].includes(location.hostname);
        $("controls-brightness").disabled=!local || !data.enabled || !data.available;
        $("controls-brightness-note").textContent=!local || !data.available ? "Panel brightness is available on the display itself when connected." : "Changes the physical display backlight.";
      } catch(_) {if(epoch===controlEpoch && mode==="controls") {$("controls-brightness").disabled=true;$("controls-brightness-note").textContent="Panel brightness is currently unavailable.";}}
    }

    async function transport(action) {
      if(controlPending)return;controlPending=true;updateControls();$("controls-status").textContent="";
      const epoch=controlEpoch;
      try {const ok=await bridge.control(action);if(epoch===controlEpoch && mode==="controls" && !ok)$("controls-status").textContent="The player didn't respond. Please try again.";}
      finally {if(epoch===controlEpoch) {controlPending=false;updateControls();}}
    }

    // Inset the optical edge from the clip, keeping the smallest sleeves whole.
    function lensOptions() {return {width:Math.max(1,map.clientWidth-32),height:Math.max(1,map.clientHeight-32),reduceMotion:reducedMotion.matches};}
    function mapBounds() {
      const xs=mapCovers.map(cover=>cover.point.x*zoom), ys=mapCovers.map(cover=>cover.point.y*zoom);
      return {minX:-Math.max(0,...xs),maxX:-Math.min(0,...xs),minY:-Math.max(0,...ys),maxY:-Math.min(0,...ys)};
    }
    function clampMap() {
      const bounds=mapBounds();
      panX=Math.max(bounds.minX,Math.min(bounds.maxX,panX));panY=Math.max(bounds.minY,Math.min(bounds.maxY,panY));
    }
    function stopMapMotion() {
      if(mapFrame!==null) cancelAnimationFrame(mapFrame);
      mapFrame=null;mapMotion=null;map.classList.remove("dragging");
      for(const cover of mapCovers)cover.el.classList.remove("pressed");
    }
    function resetMap() {stopMapMotion();pointers.clear();drag=null;zoom=1;panX=0;panY=0;positionMap();}
    function positionMap() {
      const options={...lensOptions(),panX,panY,zoom};
      for(const cover of mapCovers) {
        const projected=projectLibraryCover(cover.point,options);
        cover.el.style.transform=`translate3d(${projected.x.toFixed(3)}px,${projected.y.toFixed(3)}px,0) scale(${projected.scale.toFixed(4)})`;
        cover.el.style.opacity=projected.visible ? "1" : "0";
        cover.el.style.pointerEvents=projected.visible && projected.scale*144>=14 ? "auto" : "none";
      }
      $("explore-zoom-out").disabled=zoom<=.6;$("explore-zoom-in").disabled=zoom>=1.5;
    }
    function queueMapFrame() {if(mapFrame===null)mapFrame=requestAnimationFrame(animateMapFrame);}
    function animateMapFrame(time) {
      mapFrame=null;
      if(mode!=="explore" || selected || layout!=="mosaic" || searchKeyboard.isOpen() || document.hidden) {stopMapMotion();return;}
      if(mapMotion) {
        if(mapMotion.kind==="centre") {
          const t=Math.min(1,(time-mapMotion.started)/280),ease=1-Math.pow(1-t,3);
          panX=mapMotion.x*(1-ease);panY=mapMotion.y*(1-ease);zoom=mapMotion.zoom+(1-mapMotion.zoom)*ease;
          if(t>=1)mapMotion=null;
        } else {
          const next=stepLibraryMotion(mapMotion,mapBounds(),(time-mapFrameTime)/1000);
          panX=next.x;panY=next.y;mapMotion=next.done ? null : next;
        }
        mapFrameTime=time;movedUntil=time+120;
      }
      positionMap();
      if(mapMotion)queueMapFrame();
    }
    function startMapMotion(vx,vy) {
      if(reducedMotion.matches) {clampMap();positionMap();return;}
      mapFrameTime=performance.now();
      mapMotion={x:panX,y:panY,vx,vy};queueMapFrame();
    }
    function centreMap() {
      stopMapMotion();pointers.clear();drag=null;
      if(reducedMotion.matches) {resetMap();return;}
      mapMotion={kind:"centre",x:panX,y:panY,zoom,started:performance.now()};queueMapFrame();
    }
    function setZoom(value,anchor) {
      stopMapMotion();pointers.clear();drag=null;
      const next=zoomLibraryAt({panX,panY,zoom},value,anchor,lensOptions());
      panX=next.panX;panY=next.panY;zoom=next.zoom;clampMap();positionMap();
    }
    function mapPoint(event) {
      const rect=map.getBoundingClientRect();
      return {x:(event.clientX-rect.left-rect.width/2)*map.clientWidth/rect.width,y:(event.clientY-rect.top-rect.height/2)*map.clientHeight/rect.height};
    }
    function rubberMap(value,min,max) {
      const edge=Math.max(min,Math.min(max,value)),over=value-edge;
      return edge+Math.sign(over)*90*(1-1/(Math.abs(over)/90+1));
    }
    function pointerGeometry() {const a=[...pointers.values()];return a.length>1 ? {x:(a[0].x+a[1].x)/2,y:(a[0].y+a[1].y)/2,d:Math.hypot(a[0].x-a[1].x,a[0].y-a[1].y)} : {...a[0],d:0};}
    function beginMapPointer(event) {
      const interrupted=!!mapMotion;stopMapMotion();
      pointers.set(event.pointerId,mapPoint(event));const g=pointerGeometry();
      const cover=event.target.closest(".map-cover");
      drag={...g,panX,panY,zoom,moved:pointers.size>1 || interrupted,pinched:pointers.size>1,uri:cover && cover.dataset.uri,vx:0,vy:0,lastX:panX,lastY:panY,lastTime:performance.now()};
      if(cover && !drag.moved)cover.classList.add("pressed");
      map.setPointerCapture(event.pointerId);event.preventDefault();
    }
    function moveMapPointer(event) {
      if(!pointers.has(event.pointerId)||!drag)return;
      pointers.set(event.pointerId,mapPoint(event));const g=pointerGeometry();
      if(Math.hypot(g.x-drag.x,g.y-drag.y)>7 || (g.d && Math.abs(g.d-drag.d)>7))drag.moved=true;
      if(!drag.moved)return;
      panX=drag.panX+g.x-drag.x;panY=drag.panY+g.y-drag.y;
      if(g.d && drag.d) {
        const next=zoomLibraryAt(drag,drag.zoom*g.d/drag.d,{x:drag.x,y:drag.y},lensOptions());
        const before=unprojectLibraryPoint(drag,lensOptions()),after=unprojectLibraryPoint(g,lensOptions());
        zoom=next.zoom;panX=next.panX+after.x-before.x;panY=next.panY+after.y-before.y;
      }
      const bounds=mapBounds();panX=rubberMap(panX,bounds.minX,bounds.maxX);panY=rubberMap(panY,bounds.minY,bounds.maxY);
      const now=performance.now(), dt=(now-drag.lastTime)/1000;
      if(dt>0) {
        const weight=1-Math.exp(-dt*35);
        drag.vx+=(Math.max(-1800,Math.min(1800,(panX-drag.lastX)/dt))-drag.vx)*weight;
        drag.vy+=(Math.max(-1800,Math.min(1800,(panY-drag.lastY)/dt))-drag.vy)*weight;
      }
      drag.lastX=panX;drag.lastY=panY;drag.lastTime=now;
      for(const cover of mapCovers)cover.el.classList.remove("pressed");
      movedUntil=now+400;map.classList.add("dragging");queueMapFrame();
    }
    function endMapPointer(event) {
      if(!pointers.has(event.pointerId))return;
      const ended=drag,wasDrag=drag && drag.moved,tapUri=drag && drag.uri;
      const cancelled=event.type!=="pointerup";
      if(cancelled) {pointers.clear();drag=null;stopMapMotion();clampMap();positionMap();movedUntil=performance.now()+400;return;}
      pointers.delete(event.pointerId);if(wasDrag)movedUntil=performance.now()+400;
      if(pointers.size) {const g=pointerGeometry();drag={...g,panX,panY,zoom,moved:true,pinched:true,vx:0,vy:0,lastX:panX,lastY:panY,lastTime:performance.now()};}
      else {
        drag=null;map.classList.remove("dragging");
        for(const cover of mapCovers)cover.el.classList.remove("pressed");
        if(wasDrag) {
          const recent=ended && !ended.pinched && performance.now()-ended.lastTime<90;
          startMapMotion(recent ? ended.vx : 0,recent ? ended.vy : 0);
        }
        if(event.type==="pointerup" && !wasDrag && tapUri && performance.now()>=movedUntil) {
          const item=items.find(record=>record.uri===tapUri);if(item){movedUntil=performance.now()+400;showDetail(item,mapCovers.find(cover=>cover.item.uri===tapUri)?.el);}
        }
      }
    }
    map.addEventListener("pointerdown",beginMapPointer);map.addEventListener("pointermove",moveMapPointer);
    map.addEventListener("pointerup",endMapPointer);map.addEventListener("pointercancel",endMapPointer);map.addEventListener("lostpointercapture",endMapPointer);
    map.addEventListener("wheel",event=>{event.preventDefault();setZoom(zoom-event.deltaY*.001,mapPoint(event));},{passive:false});
    $("explore-zoom-in").addEventListener("click",()=>setZoom(zoom+.15));$("explore-zoom-out").addEventListener("click",()=>setZoom(zoom-.15));$("explore-recenter").addEventListener("click",centreMap);
    document.addEventListener("visibilitychange",()=>{if(document.hidden) {stopMapMotion();pointers.clear();drag=null;clampMap();positionMap();}});
    reducedMotion.addEventListener("change",()=>{cancelDetailMotion();stopMapMotion();pointers.clear();drag=null;clampMap();positionMap();});
    grid.addEventListener("scroll",()=>{if(mode==="explore" && layout==="grid" && !selected && !$("explore-results").hidden)gridScrollTop=grid.scrollTop;},{passive:true});
    $("explore-search").addEventListener("input",()=>{page=0;gridScrollTop=0;resetMap();render();});
    $("explore-search").addEventListener("click",openSearchKeyboard);
    $("explore-search").addEventListener("keydown",event=>{
      if(event.key==="Enter") {event.preventDefault();if(searchKeyboard.isOpen())hideSearchKeyboard(true);else openSearchKeyboard();}
    });
    $("explore-tabs").addEventListener("keydown",event=>{
      if(!["ArrowLeft","ArrowRight","Home","End"].includes(event.key))return;
      event.preventDefault();const tabs=[...$("explore-tabs").querySelectorAll("button")],index=tabs.indexOf(document.activeElement);
      const next=event.key==="Home"?0:event.key==="End"?tabs.length-1:(index+(event.key==="ArrowRight"?1:-1)+tabs.length)%tabs.length;tabs[next].click();
    });
    for(const value of ["mosaic","grid"])$(value==="mosaic"?"explore-mosaic":"explore-grid-view").addEventListener("click",()=>{layout=value;page=0;resetMap();try{localStorage.recordBrowserLayout=value;}catch(_){}render();});
    $("explore-prev").addEventListener("click",()=>{page--;resetMap();render();});$("explore-next").addEventListener("click",()=>{page++;resetMap();render();});
    $("explore-surprise").addEventListener("click",()=>{const choices=results().filter(item=>item.playable!==false);if(choices.length)showDetail(choices[Math.floor(Math.random()*choices.length)]);});
    $("explore-retry").addEventListener("click",()=>refreshLibrary(section==="made_for_you"));$("detail-retry").addEventListener("click",loadDetail);
    $("detail-back").addEventListener("click",backToBrowser);$("detail-play").addEventListener("click",()=>playSelection());
    $("detail-queue").addEventListener("click",()=>playSelection(undefined,true));
    $("explore-add-vinyl").addEventListener("click",()=>{cancelDetailMotion();stopMapMotion();hideSearchKeyboard();collectionEditor.open();});
    $("detail-add-vinyl").addEventListener("click",()=>{if(selected){cancelDetailMotion();collectionEditor.open(selected);}});
    $("open-records").addEventListener("click",()=>show("explore"));$("open-controls").addEventListener("click",()=>show("controls"));
    $("controls-records").addEventListener("click",()=>show("explore"));
    $("controls-personal").addEventListener("click",()=>{show("explore");section="made_for_you";layout="grid";page=0;gridScrollTop=0;resetMap();render();});
    $("controls-tracks").addEventListener("click",()=>{close();bridge.openTracks();});
    $("explore-close").addEventListener("click",()=>close());$("controls-close").addEventListener("click",()=>close());
    $("controls-play").addEventListener("click",()=>transport("play-pause"));$("controls-next").addEventListener("click",()=>transport("next"));$("controls-previous").addEventListener("click",()=>transport("previous"));
    $("controls-volume").addEventListener("input",()=>{bridge.setVolume(Number($("controls-volume").value));$("controls-volume-value").textContent=$("controls-volume").value+"%";});
    $("controls-brightness").addEventListener("input",()=>{bridge.setBrightness(Number($("controls-brightness").value));$("controls-brightness-value").textContent=$("controls-brightness").value+"%";});
    $("controls-seek").addEventListener("input",()=>{$("controls-position").textContent=formatTime(Number($("controls-seek").value)/1000*bridge.getState().durationMs);});
    $("controls-seek").addEventListener("change",()=>bridge.seek(Number($("controls-seek").value)/1000*bridge.getState().durationMs));
    for(const modal of [explore,controls]) {
      modal.addEventListener("pointerdown",event=>{bridge.activity();event.stopPropagation();});
      modal.addEventListener("keydown",event=>{
        bridge.activity();event.stopPropagation();
        if(event.key==="Escape") {event.preventDefault();if(searchKeyboard.isOpen())hideSearchKeyboard(true);else if(selected && !playPending)backToBrowser();else close();return;}
        if(event.key!=="Tab")return;
        const focusable=[...modal.querySelectorAll("button:not(:disabled),a,input:not(:disabled),summary")].filter(el=>el.tabIndex>=0 && !el.closest("[hidden],[inert]") && el.offsetParent!==null);
        const first=focusable[0],last=focusable[focusable.length-1];
        if(!first)return;
        if(event.shiftKey && document.activeElement===first){event.preventDefault();last.focus();}
        else if(!event.shiftKey && document.activeElement===last){event.preventDefault();first.focus();}
      });
    }
    return {openRecords:()=>show("explore"),openControls:()=>show("controls"),close,isOpen,invalidateProfile};
  };
})(typeof window !== "undefined" ? window : null);
