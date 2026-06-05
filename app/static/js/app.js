function chart(id, labels, values, type='bar'){
  const el=document.getElementById(id); if(!el) return;
  new Chart(el,{type,data:{labels,datasets:[{label:'Ventas',data:values,borderWidth:2}]},options:{plugins:{legend:{display:false}},scales:{y:{beginAtZero:true}}}});
}
chart('topProducts', window.topLabels||[], window.topValues||[]);
chart('categoryChart', window.catLabels||[], window.catValues||[], 'doughnut');
