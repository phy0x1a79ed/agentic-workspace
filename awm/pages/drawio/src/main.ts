import { mount } from 'svelte';
// The only globally-imported stylesheet, and it is load-bearing: it carries the
// reset (without it the UA's 8px body margin frames the dark app in white) and
// sets html/body background + height, so the page fills the viewport instead of
// ending where its content does.
import '@awm/primitives/tokens.css';
import App from './App.svelte';

const target = document.getElementById('app');
if (!target) throw new Error('drawio: #app not found');

mount(App, { target });
