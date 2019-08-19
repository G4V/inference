"""
mlperf inference benchmarking tool
"""

from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import argparse
import array
import collections
import json
import logging
import os
import threading
import time
from queue import Queue

import mlperf_loadgen as lg
import numpy as np

import dataset
import imagenet
import coco

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")

NANO_SEC = 1e9
MILLI_SEC = 1000

# pylint: disable=missing-docstring

SCENARIO_MAP = {
    "SingleStream": lg.TestScenario.SingleStream,
    "MultiStream": lg.TestScenario.MultiStream,
    "Server": lg.TestScenario.Server,
    "Offline": lg.TestScenario.Offline,
}
# the datasets we support
SUPPORTED_DATASETS = {
    "imagenet":
        (imagenet.Imagenet, dataset.pre_process_vgg, dataset.PostProcessCommon(offset=-1),
         {"image_size": [224, 224, 3]}),
    "imagenet_mobilenet":
        (imagenet.Imagenet, dataset.pre_process_mobilenet, dataset.PostProcessArgMax(offset=-1),
         {"image_size": [224, 224, 3]}),
    "coco-standard":
        (coco.Coco, dataset.pre_process_coco_mobilenet, coco.PostProcessCoco(),
	 {}),
#         {"image_size": [params["RESIZE_HEIGHT_SIZE"],params["RESIZE_WIDTH_SIZE"], 3]}),
    "coco-300":
        (coco.Coco, dataset.pre_process_coco_mobilenet, coco.PostProcessCoco(),
         {"image_size": [300, 300, 3]}),
    "coco-yolo":
        (coco.Coco, dataset.pre_process_coco_yolo, coco.PostProcessCoco(),
         {"image_size": [416, 416, 3]}),
    "coco-300-pt":
        (coco.Coco, dataset.pre_process_coco_pt_mobilenet, coco.PostProcessCocoPt(False,0.3),
         {"image_size": [300, 300, 3]}),         
    "coco-1200":
        (coco.Coco, dataset.pre_process_coco_resnet34, coco.PostProcessCoco(),
         {"image_size": [1200, 1200, 3]}),
    "coco-1200-onnx":
        (coco.Coco, dataset.pre_process_coco_resnet34, coco.PostProcessCocoOnnx(),
         {"image_size": [1200, 1200, 3]}),
    "coco-1200-pt":
        (coco.Coco, dataset.pre_process_coco_resnet34, coco.PostProcessCocoPt(True,0.05),
         {"image_size": [1200, 1200, 3]}),
    "coco-1200-tf":
        (coco.Coco, dataset.pre_process_coco_resnet34, coco.PostProcessCocoTf(),
         {"image_size": [1200, 1200, 3]}),
}

# pre-defined command line options so simplify things. They are used as defaults and can be
# overwritten from command line
DEFAULT_LATENCY = "0.100"
LATENCY_RESNET50 = "0.015"
LATENCY_MOBILENET = "0.010"
LATENCY_SSD_MOBILENET = "0.010"
 # FIXME: change once final value is known
LATENCY_SSD_RESNET34 = "0.100"

SUPPORTED_PROFILES = {
    "default_tf_object_det_zoo": {
        "inputs": "image_tensor:0",
        "outputs": "num_detections:0,detection_boxes:0,detection_scores:0,detection_classes:0",
        "dataset": "coco-standard",
        "backend": "tensorflow",
    },
    "default_tf_trt_object_det_zoo": {
        "inputs": "import/image_tensor:0",
        "outputs": "import/num_detections:0,import/detection_boxes:0,import/detection_scores:0,import/detection_classes:0",
        "dataset": "coco-standard",
        "backend": "tensorflowRT",
    },
    "tf_yolo": {
        "inputs": "input/input_data:0",
        "outputs": "pred_sbbox/concat_2:0,pred_mbbox/concat_2:0,pred_lbbox/concat_2:0",
        "dataset": "coco-yolo",
        "backend": "tensorflow",
    },
    "tf_yolo_trt": {
        "inputs": "import/input/input_data:0",
        "outputs": "import/pred_sbbox/concat_2:0,import/pred_mbbox/concat_2:0,import/pred_lbbox/concat_2:0",
        "dataset": "coco-yolo",
        "backend": "tensorflowRT",
    },
    # resnet
    "resnet50-tf": {
        "inputs": "input_tensor:0",
        "outputs": "ArgMax:0",
        "dataset": "imagenet",
        "backend": "tensorflow",
        "max-latency": LATENCY_RESNET50,
    },
    "resnet50-onnxruntime": {
        "dataset": "imagenet",
        "outputs": "ArgMax:0",
        "backend": "onnxruntime",
        "max-latency": LATENCY_RESNET50,
    },

    # mobilenet
    "mobilenet-tf": {
        "inputs": "input:0",
        "outputs": "MobilenetV1/Predictions/Reshape_1:0",
        "dataset": "imagenet_mobilenet",
        "backend": "tensorflow",
        "max-latency": LATENCY_MOBILENET,
    },
    "mobilenet-onnxruntime": {
        "dataset": "imagenet_mobilenet",
        "outputs": "MobilenetV1/Predictions/Reshape_1:0",
        "backend": "onnxruntime",
        "max-latency": LATENCY_MOBILENET,
    },

    # ssd-mobilenet
    "ssd-mobilenet-tf": {
        "inputs": "image_tensor:0",
        "outputs": "num_detections:0,detection_boxes:0,detection_scores:0,detection_classes:0",
        "dataset": "coco-300",
        "backend": "tensorflow",
        "max-latency": LATENCY_SSD_MOBILENET,
    },
    "ssd-mobilenet-pytorch": {
        "inputs": "image",
        "outputs": "bboxes,labels,scores",
        "dataset": "coco-300-pt",
        "backend": "pytorch-native",
        "max-latency": LATENCY_SSD_MOBILENET,
    },
    "ssd-mobilenet-onnxruntime": {
        "dataset": "coco-300",
        "outputs": "num_detections:0,detection_boxes:0,detection_scores:0,detection_classes:0",
        "backend": "onnxruntime",        
        "data-format": "NHWC",
        "max-latency": LATENCY_SSD_MOBILENET,
    },

    # ssd-resnet34
    "ssd-resnet34-tf": {
        "inputs": "image:0",
        "outputs": "detection_bboxes:0,detection_classes:0,detection_scores:0",
        "dataset": "coco-1200-tf",
        "backend": "tensorflow",
        "data-format": "NCHW",
        "max-latency": LATENCY_SSD_RESNET34,
    },
    "ssd-resnet34-pytorch": {
        "inputs": "image",
        "outputs": "bboxes,labels,scores",
        "dataset": "coco-1200-pt",
        "backend": "pytorch-native",
        "max-latency": LATENCY_SSD_RESNET34,
    },
    "ssd-resnet34-onnxruntime": {
        "dataset": "coco-1200-onnx",
        "inputs": "image",
        "outputs": "bboxes,labels,scores",
        "backend": "onnxruntime",
        "data-format": "NCHW",
        "max-batchsize": 1,
        "max-latency": LATENCY_SSD_RESNET34,
    },
    "ssd-resnet34-onnxruntime-tf": {
        "dataset": "coco-1200-tf",
        "inputs": "image:0",
        "outputs": "detection_bboxes:0,detection_classes:0,detection_scores:0",
        "backend": "onnxruntime",
        "data-format": "NHWC",
        "max-latency": LATENCY_SSD_RESNET34,
    },
}


last_timeing = []


def get_args(params):
    """Parse commandline.
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", choices=SUPPORTED_DATASETS.keys(), help="dataset")
    parser.add_argument("--dataset-path", required=True, help="path to the dataset")
    parser.add_argument("--dataset-list", help="path to the dataset list")
    parser.add_argument("--data-format", choices=["NCHW", "NHWC"], help="data format")
    parser.add_argument("--profile", choices=SUPPORTED_PROFILES.keys(), help="standard profiles")
    parser.add_argument("--scenario", default="SingleStream",
                        help="mlperf benchmark scenario, list of " + str(list(SCENARIO_MAP.keys())))
    parser.add_argument("--queries-single", type=int, default=1024,
                        help="mlperf number of queries for SingleStream")
    parser.add_argument("--queries-offline", type=int, default=24576,
                        help="mlperf number of queries for Offline")
    parser.add_argument("--queries-multi", type=int, default=24576,
                        help="mlperf number of queries for MultiStream,Server")
    parser.add_argument("--max-batchsize", type=int,
                        help="max batch size in a single inference")
    parser.add_argument("--model", required=True, help="model file")
    parser.add_argument("--output", help="test results")
    parser.add_argument("--inputs", help="model inputs")
    parser.add_argument("--outputs", help="model outputs")
    parser.add_argument("--backend", help="runtime to use")
    parser.add_argument("--threads", default=os.cpu_count(), type=int, help="threads")
    parser.add_argument("--time", type=int, help="time to scan in seconds")
    parser.add_argument("--count", type=int, help="dataset items to use")
    parser.add_argument("--qps", type=int, default=10, help="target qps estimate")
    parser.add_argument("--max-latency", type=str, help="mlperf max latency in 99pct tile")
    parser.add_argument("--cache", type=int, default=0, help="use cache")
    parser.add_argument("--accuracy", action="store_true", help="enable accuracy pass")
    args = parser.parse_args()
    """

    # don't use defaults in argparser. Instead we default to a dict, override that with a profile
    # and take this as default unless command line give
    defaults = SUPPORTED_PROFILES["default_tf_object_det_zoo"]

    if params["PROFILE"] != 'default_tf_object_det_zoo':
        profile = SUPPORTED_PROFILES[params["PROFILE"]]
        defaults.update(profile)
#    for k, v in defaults.items():
#        kc = k.replace("-", "_")
#        if getattr(args, kc) is None:
#            setattr(args, kc, v)
#    if args.inputs:
#        args.inputs = args.inputs.split(",")
#    if args.outputs:
#        args.outputs = args.outputs.split(",")
#TODO redo the split of string to have array with diff latencies
#    if args.max_latency:
    params["MAX_LATENCY"] = [float(i) for i in params["MAX_LATENCY"].split(",")]
    try:
        params["SCENARIO"] =[SCENARIO_MAP[scenario] for scenario in params["SCENARIO"].split(",")] 
    except:
        parser.error("valid scanarios:" + str(list(SCENARIO_MAP.keys())))
    
#    if params["TIME"] != 0:
#        defaults['time'] = params["TIME"]
#    if params["QPS"] != 0:
#        defaults['qps'] = params["QPS"]
#    if params["ACCURACY"] != False:
#        defaults['accuracy'] = params["ACCURACY"]
#    if params["NUM_THREADS"] != 1:
#        defaults['num_threads'] = params["NUM_THREADS"]
    #split tensor string into arrays
    defaults['inputs'] =  defaults['inputs'].split(",") 
    defaults['outputs'] = defaults['outputs'].split(",") 

    return defaults


def get_backend(backend):
    if backend == "tensorflow":
        from backend_tf import BackendTensorflow
        backend = BackendTensorflow()
    elif backend == "tensorflowRT":
        from backend_tf_trt import BackendTensorflowRT
        backend = BackendTensorflowRT()
    elif backend == "onnxruntime":
        from backend_onnxruntime import BackendOnnxruntime
        backend = BackendOnnxruntime()
    elif backend == "null":
        from backend_null import BackendNull
        backend = BackendNull()
    elif backend == "pytorch":
        from backend_pytorch import BackendPytorch
        backend = BackendPytorch()
    elif backend == "pytorch-native":
        from backend_pytorch_native import BackendPytorchNative
        backend = BackendPytorchNative()      
    elif backend == "tflite":
        from backend_tflite import BackendTflite
        backend = BackendTflite()
    else:
        raise ValueError("unknown backend: " + backend)
    return backend


class Item:
    """An item that we queue for processing by the thread pool."""

    def __init__(self, query_id, content_id, img, label=None):
        self.query_id = query_id
        self.content_id = content_id
        self.img = img
        self.label = label
        self.start = time.time()


class RunnerBase:
    def __init__(self, model, ds, threads, post_proc=None, max_batchsize=128):
        self.take_accuracy = False
        self.ds = ds
        self.model = model
        self.post_process = post_proc
        self.threads = threads
        self.take_accuracy = False
        self.max_batchsize = max_batchsize
        self.result_timing = []
        self.batch_count = 0
#        self.feeds = []
#        self.ids = []
#        self.results = []

    def handle_tasks(self, tasks_queue):
        pass

    def start_run(self, result_dict, take_accuracy):
        self.result_dict = result_dict
        self.result_timing = []
        self.take_accuracy = take_accuracy
        self.post_process.start()

    def run_one_item(self, qitem):
        # run the prediction
        processed_results = []
        try:
            feed = {self.model.inputs[0]: qitem.img}
 #           self.feeds.append(feed)
 #           self.ids.append(qitem.content_id)
            results = self.model.predict(feed)
            #results = self.model.predict({self.model.inputs[0]: qitem.img})
            print("flag batch done", self.batch_count)
            self.batch_count += 1
            processed_results = self.post_process(results, qitem.content_id, qitem.label, self.result_dict)
            print("flag batch postprocessed")
#            self.results.append(results)
            if self.take_accuracy:
                self.post_process.add_results(processed_results)
                self.result_timing.append(time.time() - qitem.start)
        except Exception as ex:  # pylint: disable=broad-except
            src = [self.ds.get_item_loc(i) for i in qitem.content_id]
            log.error("thread: failed on contentid=%s, %s", src, ex)
            # since post_process will not run, fake empty responses
            processed_results = [[]] * len(qitem.query_id)
        finally:
            response_array_refs = []
            response = []
            for idx, query_id in enumerate(qitem.query_id):
                response_array = array.array("B", np.array(processed_results[idx], np.float32).tobytes())
                response_array_refs.append(response_array)## what is this for????????
                bi = response_array.buffer_info()
                response.append(lg.QuerySampleResponse(query_id, bi[0], bi[1]))
            lg.QuerySamplesComplete(response)

    def enqueue(self, query_samples):
        idx = [q.index for q in query_samples]
        query_id = [q.id for q in query_samples]
        if len(query_samples) < self.max_batchsize:
            data, label = self.ds.get_samples(idx)
            self.run_one_item(Item(query_id, idx, data, label))
        else:
            bs = self.max_batchsize
            for i in range(0, len(idx), bs):
                data, label = self.ds.get_samples(idx[i:i+bs])
                self.run_one_item(Item(query_id[i:i+bs], idx[i:i+bs], data, label))

    def finish(self):
        pass


class QueueRunner(RunnerBase):
    def __init__(self, model, ds, threads, post_proc=None, max_batchsize=128):
        super().__init__(model, ds, threads, post_proc, max_batchsize)
        self.tasks = Queue(maxsize=threads * 4)
        self.workers = []
        self.result_dict = {}

        for _ in range(self.threads):
            worker = threading.Thread(target=self.handle_tasks, args=(self.tasks,))
            worker.daemon = True
            self.workers.append(worker)
            worker.start()

    def handle_tasks(self, tasks_queue):
        """Worker thread."""
        while True:
            qitem = tasks_queue.get()
            if qitem is None:
                # None in the queue indicates the parent want us to exit
                tasks_queue.task_done()
                break
            self.run_one_item(qitem)
            tasks_queue.task_done()

    def enqueue(self, query_samples):
        idx = [q.index for q in query_samples]
        query_id = [q.id for q in query_samples]
        if len(query_samples) < self.max_batchsize:
            data, label = self.ds.get_samples(idx)
            self.tasks.put(Item(query_id, idx, data, label))
        else:
            bs = self.max_batchsize
            for i in range(0, len(idx), bs):
                ie = i + bs
                data, label = self.ds.get_samples(idx[i:ie])
                self.tasks.put(Item(query_id[i:ie], idx[i:ie], data, label))

    def finish(self):
        # exit all threads
        for _ in self.workers:
            self.tasks.put(None)
        for worker in self.workers:
            worker.join()


def add_results(final_results, name, result_dict, result_list, took, show_accuracy=False):
    percentiles = [50., 80., 90., 95., 99., 99.9]
    buckets = np.percentile(result_list, percentiles).tolist()
    buckets_str = ",".join(["{}:{:.4f}".format(p, b) for p, b in zip(percentiles, buckets)])

    if result_dict["total"] == 0:
        result_dict["total"] = len(result_list)

    # this is what we record for each run
    result = {
        "took": took,
        "mean": np.mean(result_list),
        "percentiles": {str(k): v for k, v in zip(percentiles, buckets)},
        "qps": len(result_list) / took,
        "count": len(result_list),
        "good_items": result_dict["good"],
        "total_items": result_dict["total"],
    }
    acc_str = ""
    if show_accuracy:
        result["accuracy"] = 100. * result_dict["good"] / result_dict["total"]
        acc_str = ", acc={:.4f}".format(result["accuracy"])
        if "mAP" in result_dict:
            result["mAP"] = result_dict["mAP"]
            acc_str += ", mAP={:.15f}".format(result_dict["mAP"])

    # add the result to the result dict
    final_results[name] = result

    # to stdout
    print("{} qps={:.2f}, mean={:.4f}, time={:.2f}{}, queries={}, tiles={}".format(
        name, result["qps"], result["mean"], took, acc_str,
        len(result_list), buckets_str))


def mlperf_process(params):
    print ("inside mlperf process!")
    from pprint import pprint
    pprint (params)
    global last_timeing
    config = get_args(params)

    log.info(config)
    print (config)
    # find backend
    backend = get_backend(config['backend'])
    print(backend.name())
    if backend.name() == 'tensorflowRT':
        print ("##########################################################")
        backend.set_extra_params(params)
    # override image format if given
    #image_format = config['data_format'] if config['data_format'] else backend.image_format()
    image_format = backend.image_format()

    # --count applies to accuracy mode only and can be used to limit the number of images
    # for testing. For perf model we always limit count to 200.
    count = params["BATCH_COUNT"]*params["BATCH_SIZE"] 
    if not count:
#        if not args.accuracy:
        count = 1
    print ("##########################################################")
    print (count)
    # dataset to use
    if config['dataset'] != 'coco-yolo':
        wanted_dataset, pre_proc, post_proc, kwargs = SUPPORTED_DATASETS[config['dataset']]
        #wanted_dataset, pre_proc, post_proc, kwargs = SUPPORTED_DATASETS[config['dataset']]
        kwargs = {"image_size": [params["RESIZE_HEIGHT_SIZE"],params["RESIZE_WIDTH_SIZE"], 3]}
        ds = wanted_dataset(data_path=os.path.dirname(params["IMAGES_DIR"]),
                        image_list=None,
                        name=params["DATASET_TYPE"],
                        image_format=image_format,
                        pre_process=pre_proc,
                        use_cache=params["CACHE"],    #now is set to 0, is a commandline arg which i dont know whats it is.
                        count=count, **kwargs)
    # load model to backend
    else: 
        wanted_dataset, pre_proc, post_proc, kwargs = SUPPORTED_DATASETS[config['dataset']]
        #wanted_dataset, pre_proc, post_proc, kwargs = SUPPORTED_DATASETS[config['dataset']]
        kwargs = {"image_size": [params["RESIZE_HEIGHT_SIZE"],params["RESIZE_WIDTH_SIZE"], 3]}
        ds = wanted_dataset(data_path=os.path.dirname(params["IMAGES_DIR"]),
                        image_list=None,
                        name=params["DATASET_TYPE"],
                        image_format=image_format,
                        pre_process=pre_proc,
                        use_cache=params["CACHE"],    #now is set to 0, is a commandline arg which i dont know whats it is.
                        count=count, **kwargs)
        post_proc = coco.PostProcessCocoYolo(ds)


    model = backend.load(params["FROZEN_GRAPH"], inputs=config['inputs'], outputs=config['outputs'])
    final_results = {
        "runtime": model.name(),
        "version": model.version(),
        "time": int(time.time()),
        "cmdline": str("placeholder"),
    }

    #
    # make one pass over the dataset to validate accuracy
    #
    count = ds.get_item_count()
    
    # warmup
    ds.load_query_samples([0])
    for _ in range(5):
        img, _ = ds.get_samples([0])
        _ = backend.predict({backend.inputs[0]: img})
    ds.unload_query_samples(None)
    debug_feeds = []
    for scenario in params["SCENARIO"]:
        runner_map = {
            lg.TestScenario.SingleStream: RunnerBase,
            lg.TestScenario.MultiStream: QueueRunner,
            lg.TestScenario.Server: QueueRunner,
            lg.TestScenario.Offline: QueueRunner
        }
        print(scenario)
        runner = runner_map[scenario](model, ds, params["NUM_THREADS"], post_proc=post_proc, max_batchsize=params["BATCH_SIZE"])

        def issue_queries(query_samples):
            runner.enqueue(query_samples)

        def flush_queries(): pass

        def process_latencies(latencies_ns):
            # called by loadgen to show us the recorded latencies
            global last_timeing
            last_timeing = [t / NANO_SEC for t in latencies_ns]

        settings = lg.TestSettings()
        settings.scenario = scenario
        settings.mode = lg.TestMode.PerformanceOnly

        if params["ACCURACY"]:
            settings.mode = lg.TestMode.AccuracyOnly

#        if args.time:
            # override the time we want to run
        settings.min_duration_ms = params["TIME"] * MILLI_SEC
        settings.max_duration_ms = params["TIME"] * MILLI_SEC

        #if args.qps:
        qps = float(params["QPS"])
        settings.server_target_qps = qps
        settings.offline_expected_qps = qps

        if scenario == lg.TestScenario.SingleStream:
            settings.min_query_count = params["QUERIES_SINGLE"]
            settings.max_query_count = params["QUERIES_SINGLE"]
        elif scenario == lg.TestScenario.MultiStream:
            settings.min_query_count = params["QUERIES_MULTI"]
            settings.max_query_count = params["QUERIES_MULTI"]
            settings.multi_stream_samples_per_query = 4    ###was hardcoded in the original.
        elif scenario == lg.TestScenario.Server:
            max_latency = params["MAX_LATENCY"]
        elif scenario == lg.TestScenario.Offline:
            settings.min_query_count = params["QUERIES_OFFLINE"]
            settings.max_query_count = params["QUERIES_OFFLINE"]

        sut = lg.ConstructSUT(issue_queries, flush_queries, process_latencies)
        qsl = lg.ConstructQSL(count, min(count, 1000), ds.load_query_samples, ds.unload_query_samples)

        if scenario == lg.TestScenario.Server:
            for target_latency in max_latency:
                log.info("starting {}, latency={}".format(scenario, target_latency))
                settings.server_target_latency_ns = int(target_latency * NANO_SEC)

                result_dict = {"good": 0, "total": 0, "scenario": str(scenario)}
                runner.start_run(result_dict, params["ACCURACY"])
                lg.StartTest(sut, qsl, settings)

                if not last_timeing:
                    last_timeing = runner.result_timing
                if params["ACCURACY"]:
                    post_proc.finalize(result_dict, ds, output_dir=params["CUR_DIR"])
                add_results(final_results, "{}-{}".format(scenario, target_latency),
                            result_dict, last_timeing, time.time() - ds.last_loaded, params["ACCURACY"])
        else:
            log.info("starting {}".format(scenario))
            result_dict = {"good": 0, "total": 0, "scenario": str(scenario)}
            runner.start_run(result_dict, params["ACCURACY"])
            lg.StartTest(sut, qsl, settings)

            if not last_timeing:
                last_timeing = runner.result_timing
            if params["ACCURACY"]:
                post_proc.finalize(result_dict, ds, output_dir=params["CUR_DIR"])
            add_results(final_results, "{}".format(scenario),
                        result_dict, last_timeing, time.time() - ds.last_loaded, params["ACCURACY"])

        runner.finish()
#        print("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
#        debug_feeds = runner.feeds
#        debug_ids = []
#        debug_sizes = []
#        debug_results = runner.results 
#        for bid in runner.ids:
#            tmp_id = []
#            tmp_img = []
#            for iid in bid:
#                tmp_id.append(ds.image_ids[iid])
#                tmp_img.append(ds.image_sizes[iid])
#            debug_ids.append(tmp_id)
#            debug_sizes.append(tmp_img)

        lg.DestroyQSL(qsl)
        lg.DestroySUT(sut)

    #
    # write final results
    #
#    if args.output:
    with open('output.json', "w") as f:
        json.dump(final_results, f, sort_keys=True, indent=4)
#    return debug_feeds,debug_ids,debug_sizes,debug_results


if __name__ == "__main__":
    main()