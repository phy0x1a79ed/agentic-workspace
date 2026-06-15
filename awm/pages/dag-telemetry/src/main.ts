import { mount } from 'svelte';
import App from './App.svelte';

const target = document.getElementById('app');
if (!target) throw new Error('dag-telemetry: #app not found');

mount(App, { target });
