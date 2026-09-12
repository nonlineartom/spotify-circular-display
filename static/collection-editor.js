/* Catalogue search and explicit additions to the Pi's shared physical collection. */
(function(root) {
  "use strict";
  root.createCollectionEditor = function(bridge) {
    const $=id=>document.getElementById(id), modal=$("vinyl-editor"), shell=document.querySelector(".explore-shell");
    const input=$("vinyl-search"), results=$("vinyl-search-results"), status=$("vinyl-editor-status");
    let open=false, epoch=0, controller=null, returnFocus=null, changed=false, pending=false;
    const keyboard=root.createTouchKeyboard({element:$("vinyl-keyboard"),onDone:search});
    function showKeyboard() {if(!open||pending)return;keyboard.open(input,{doneLabel:"Search"});input.setAttribute("aria-expanded","true");}
    function hideKeyboard() {keyboard.close();input.setAttribute("aria-expanded","false");}
    function busy(value) {
      pending=value;$("vinyl-search-submit").disabled=value;input.disabled=value;
      for(const button of results.querySelectorAll("button"))button.disabled=value||button.dataset.added==="true";
    }
    function close(restore=true) {
      if(!open)return;open=false;epoch++;if(controller)controller.abort();controller=null;hideKeyboard();
      modal.hidden=true;shell.inert=false;input.value="";results.replaceChildren();status.textContent="";busy(false);
      if(restore&&returnFocus&&returnFocus.isConnected&&!returnFocus.closest("[hidden]"))returnFocus.focus({preventScroll:true});
      if(changed)bridge.onChanged();changed=false;returnFocus=null;
    }
    async function request(url,options={}) {
      const serial=++epoch;if(controller)controller.abort();controller=new AbortController();
      const current=controller, profile=bridge.getProfile(), timeout=setTimeout(()=>current.abort(),12000);
      busy(true);
      try {
        const response=await fetch(url,{...options,cache:"no-store",signal:current.signal});
        const data=await response.json();
        if(!open||serial!==epoch||current.signal.aborted)return null;
        if(response.status===409&&data.code==="profile_changed") {bridge.onProfileChanged(data,profile.revision);close(false);return null;}
        if(!response.ok)throw new Error(data.error||"The collection could not be updated. Try again.");
        if(bridge.getProfile().epoch!==profile.epoch) {close(false);return null;}
        if(!bridge.acceptProfile(data,profile.revision)) {close(false);return null;}
        return data;
      } catch(error) {
        if(open&&serial===epoch)status.textContent=error.name==="AbortError"?"Spotify took too long to respond. Please try again.":error.message;
        return null;
      } finally {clearTimeout(timeout);if(serial===epoch){controller=null;busy(false);}}
    }
    async function add(item,button) {
      if(pending)return;hideKeyboard();status.textContent=`Adding ${item.title}…`;
      // Refresh even if the screen closes after the server has saved the entry.
      changed=true;
      const data=await request("/api/vinyls",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({uri:item.uri,profile_epoch:bridge.getProfile().epoch})});
      if(!data)return;
      button.textContent="In Vinyls ✓";button.dataset.added="true";button.disabled=true;
      button.setAttribute("aria-label",`${data.item.title} is in Vinyls`);
      status.textContent=data.added?`${data.item.title} is now in Vinyls.`:`${data.item.title} is already in Vinyls.`;
      bridge.onChanged();
    }
    function render(items) {
      results.replaceChildren(...items.map(item=>{
        const row=document.createElement("article");row.className="vinyl-result";
        const art=document.createElement("div");art.className="vinyl-result-art";art.textContent="◎";
        if(/^https:\/\/i\.scdn\.co\/image\/|^\/static\//.test(item.image||"")) {
          const img=document.createElement("img");img.src=item.image;img.alt="";img.loading="lazy";img.addEventListener("error",()=>{art.textContent="◎";},{once:true});art.replaceChildren(img);
        }
        const copy=document.createElement("div");copy.className="vinyl-result-copy";
        const title=document.createElement("strong"),meta=document.createElement("p");title.textContent=item.title;
        meta.textContent=[item.subtitle,item.release_date].filter(Boolean).join(" · ");copy.append(title,meta);
        const button=document.createElement("button");button.type="button";button.className="utility-button";
        button.textContent=item.in_collection?"In Vinyls ✓":"+ Add to Vinyls";button.dataset.added=String(!!item.in_collection);button.disabled=!!item.in_collection;
        button.setAttribute("aria-label",item.in_collection?`${item.title} is in Vinyls`:`Add ${item.title} to Vinyls`);
        button.addEventListener("click",()=>add(item,button));row.append(art,copy,button);return row;
      }));results.scrollTop=0;
    }
    async function search() {
      if(!open||pending)return;
      const query=input.value.trim();if(query.length<2){status.textContent="Enter at least two letters to find a record.";showKeyboard();return;}
      hideKeyboard();results.replaceChildren();status.textContent="Finding Spotify editions…";
      const params=new URLSearchParams({q:query,profile_epoch:bridge.getProfile().epoch||""});
      const data=await request(`/api/vinyls/search?${params}`);
      if(!data)return;
      render(data.items||[]);status.textContent=data.items?.length?"Choose the edition that matches your record.":"No albums found. Try the artist and album name together.";
    }
    $("vinyl-search-form").addEventListener("submit",event=>{event.preventDefault();search();});
    input.addEventListener("click",showKeyboard);
    $("vinyl-editor-close").addEventListener("click",()=>close());$("vinyl-editor-done").addEventListener("click",()=>close());
    modal.addEventListener("keydown",event=>{
      event.stopPropagation();bridge.activity();
      if(event.key==="Escape"){event.preventDefault();if(keyboard.isOpen())hideKeyboard();else close();}
      if(event.key!=="Tab")return;
      const nodes=[...modal.querySelectorAll("button:not(:disabled),input:not(:disabled)")].filter(el=>!el.closest("[hidden]")&&el.offsetParent!==null);
      if(event.shiftKey&&document.activeElement===nodes[0]){event.preventDefault();nodes.at(-1)?.focus();}
      else if(!event.shiftKey&&document.activeElement===nodes.at(-1)){event.preventDefault();nodes[0]?.focus();}
    });
    return {close,isOpen:()=>open,open(item=null){
      close(false);returnFocus=document.activeElement;open=true;modal.hidden=false;shell.inert=true;status.textContent="";
      $("vinyl-editor-done").textContent=item?"Back to album":"Back to exploring";
      if(item){render([{...item,in_collection:(item.sections||[]).includes("vinyls")}]);status.textContent="Add this record to the Pi’s shared collection.";$("vinyl-editor-close").focus({preventScroll:true});}
      else {status.textContent="Use the keys below to search by album or artist.";showKeyboard();}
    }};
  };
})(window);
