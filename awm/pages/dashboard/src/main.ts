import { mount } from 'svelte';
import App from './App.svelte';
import '@awm/primitives/tokens.css';

const target = document.getElementById('app');
if (!target) throw new Error('dashboard: #app not found');

mount(App, { target });
