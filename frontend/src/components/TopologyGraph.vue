<script setup lang="ts">
import { ref, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { GraphChart, type GraphSeriesOption } from 'echarts/charts'
import {
  LegendComponent,
  TooltipComponent,
  type LegendComponentOption,
  type TooltipComponentOption,
} from 'echarts/components'
import { init, use, type ComposeOption, type ECharts } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { API_BASE_URL } from '../api'

// ECharts 的默认入口会把所有图表和渲染器打入首屏包。这里按拓扑图真实能力注册，
// 既保留 force graph，又让 Vite/Rolldown 能删除项目从未使用的图表实现。
use([GraphChart, LegendComponent, TooltipComponent, CanvasRenderer])
type TopologyChartOption = ComposeOption<
  GraphSeriesOption | LegendComponentOption | TooltipComponentOption
>

const props = defineProps<{
  theme: 'light' | 'dark'
}>()

interface TopologyNode {
  id: string
  name: string
  qualname: string
  type: string
  file_path: string
}

interface TopologyLink {
  source: string
  target: string
}

interface TopologyData {
  nodes: TopologyNode[]
  links: TopologyLink[]
}

const graphRef = ref<HTMLDivElement | null>(null)
let myChart: ECharts | null = null
let latestTopology: TopologyData | null = null

const loading = ref(false)
const errorMsg = ref('')
const isFallback = ref(false)

const fetchTopology = async () => {
  loading.value = true
  errorMsg.value = ''
  try {
    const res = await fetch(`${API_BASE_URL}/api/graph/topology`)
    if (!res.ok) throw new Error('无法连接后端 API')
    const data = await res.json()
    isFallback.value = data.fallback || false
    latestTopology = { nodes: data.nodes, links: data.links }

    await nextTick()
    renderChart(data.nodes, data.links)
  } catch (err: unknown) {
    console.error(err)
    errorMsg.value = err instanceof Error ? err.message : '获取图谱失败'
  } finally {
    loading.value = false
  }
}

const renderChart = (nodes: TopologyNode[], links: TopologyLink[]) => {
  if (!graphRef.value) return

  const isDark = props.theme === 'dark'
  const palette = {
    classNode: isDark ? '#60a5fa' : '#2563eb',
    functionNode: isDark ? '#2dd4bf' : '#0f766e',
    tooltipBackground: isDark ? '#151b2b' : '#ffffff',
    tooltipBorder: isDark ? '#6476ff' : '#4f46e5',
    tooltipText: isDark ? '#f8fafc' : '#172033',
    label: isDark ? '#d9e1f2' : '#334155',
    edge: isDark ? '#71809d' : '#94a3b8',
    emphasis: isDark ? '#a5b4fc' : '#4f46e5',
  }

  if (!myChart) {
    myChart = init(graphRef.value)
  }

  // 格式化节点数据
  const formattedNodes = nodes.map((node) => {
    const isClass = node.type === 'Class'
    return {
      id: node.id,
      name: node.name,
      symbolSize: isClass ? 32 : 22,
      value: node.file_path,
      category: isClass ? 0 : 1,
      itemStyle: {
        color: isClass ? palette.classNode : palette.functionNode,
      },
      tooltip: {
        formatter: `<strong>${node.qualname}</strong><br/>类型: ${node.type}<br/>位置: ${node.file_path}`,
      },
    }
  })

  // 格式化连线数据
  const formattedLinks = links.map((link) => {
    return {
      source: link.source,
      target: link.target,
      lineStyle: {
        width: 2,
        curveness: 0.15,
      },
    }
  })

  const option: TopologyChartOption = {
    tooltip: {
      trigger: 'item',
      backgroundColor: palette.tooltipBackground,
      borderColor: palette.tooltipBorder,
      borderWidth: 1,
      textStyle: {
        color: palette.tooltipText,
        fontSize: 12,
      },
    },
    legend: [
      {
        data: ['类 (Class)', '函数 (Function)'],
        textStyle: {
          color: palette.label,
        },
        top: '5%',
      },
    ],
    series: [
      {
        type: 'graph',
        layout: 'force',
        data: formattedNodes,
        links: formattedLinks,
        categories: [{ name: '类 (Class)' }, { name: '函数 (Function)' }],
        roam: true,
        label: {
          show: true,
          position: 'right',
          color: palette.label,
          fontSize: 11,
        },
        force: {
          repulsion: 200,
          edgeLength: 120,
          gravity: 0.05,
        },
        edgeSymbol: ['none', 'arrow'],
        edgeSymbolSize: [4, 8],
        lineStyle: {
          color: palette.edge,
          opacity: 0.6,
        },
        emphasis: {
          focus: 'adjacency',
          lineStyle: {
            width: 4,
            color: palette.emphasis,
          },
        },
      },
    ],
  }

  myChart.setOption(option, true)
}

watch(
  () => props.theme,
  () => {
    if (latestTopology) {
      renderChart(latestTopology.nodes, latestTopology.links)
    }
  },
)

const handleResize = () => {
  myChart?.resize()
}

let resizeObserver: ResizeObserver | null = null

onMounted(() => {
  fetchTopology()
  window.addEventListener('resize', handleResize)

  if (graphRef.value) {
    resizeObserver = new ResizeObserver(() => {
      myChart?.resize()
    })
    resizeObserver.observe(graphRef.value)
  }
})

onUnmounted(() => {
  window.removeEventListener('resize', handleResize)
  if (resizeObserver) {
    resizeObserver.disconnect()
  }
  myChart?.dispose()
})

defineExpose({
  refresh: fetchTopology,
  resize: handleResize,
})
</script>

<template>
  <div class="topology-container">
    <div class="topology-toolbar">
      <span class="title"
        ><span class="panel-icon" aria-hidden="true">⌘</span> 代码架构拓扑图谱</span
      >
      <span v-if="isFallback" class="topology-badge fallback">AST 降级模式</span>
      <span v-else class="topology-badge neo4j">Neo4j 图数据</span>
      <button @click="fetchTopology" :disabled="loading" class="refresh-btn">
        {{ loading ? '刷新中...' : '刷新图谱' }}
      </button>
    </div>

    <div v-if="errorMsg" class="error-view">
      <p class="error-text">❌ 加载图谱失败: {{ errorMsg }}</p>
      <button @click="fetchTopology" class="retry-btn">重试</button>
    </div>

    <div v-show="!errorMsg" ref="graphRef" class="graph-canvas"></div>
  </div>
</template>

<style scoped>
/*
 * 拓扑图是异步挂载的独立主题边界，直接消费 App 根节点继承下来的语义令牌。
 * 组件自身不再硬编码某个主题，也不依赖父组件用高优先级选择器进行事后修补。
 */
.topology-container {
  display: flex;
  flex-direction: column;
  height: 100%;
  width: 100%;
  background: var(--surface-muted);
}

.topology-toolbar {
  display: flex;
  align-items: center;
  gap: 0.8rem;
  min-height: 43px;
  padding: 0.68rem 0.8rem;
  background: var(--surface-muted);
  border-bottom: 1px solid var(--border);
}

.topology-toolbar .title {
  color: var(--text-soft);
  font-size: 0.84rem;
  font-weight: 720;
  letter-spacing: -0.01em;
}

.panel-icon {
  display: inline-grid;
  width: 1rem;
  place-items: center;
  color: var(--primary);
  font-family: ui-monospace, 'Cascadia Code', Consolas, monospace;
  font-weight: 800;
}

.topology-badge {
  font-size: 10px;
  padding: 2px 6px;
  border-radius: 4px;
  font-weight: 700;
}

.topology-badge.fallback {
  background: var(--warning);
  color: var(--surface);
}

.topology-badge.neo4j {
  background: var(--accent-soft);
  color: var(--accent);
}

.refresh-btn {
  margin-left: auto;
  background: var(--surface-strong);
  border: 1px solid var(--border-strong);
  color: var(--text-soft);
  padding: 3px 8px;
  font-size: 11px;
  border-radius: 6px;
  cursor: pointer;
  transition:
    background-color 160ms ease,
    color 160ms ease;
}

.refresh-btn:hover {
  background: var(--primary-soft);
  color: var(--primary);
}

.graph-canvas {
  flex: 1;
  width: 100%;
  height: 100%;
  min-height: 250px;
}

.error-view {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 1rem;
}

.error-text {
  color: var(--danger);
  font-size: 0.9rem;
}

.retry-btn {
  background: var(--primary);
  color: #fff;
  border: none;
  padding: 5px 12px;
  font-size: 12px;
  border-radius: 4px;
  cursor: pointer;
}

.retry-btn:hover {
  background: var(--primary-hover);
}
</style>
